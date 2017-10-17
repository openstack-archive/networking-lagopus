#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import eventlet
import os
import socket
import sys

from neutron_lib import context
from neutron_lib.utils import helpers
from oslo_config import cfg
from oslo_log import helpers as log_helpers
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_service import service
from osprofiler import profiler

from neutron.agent.linux import ip_lib
from neutron.agent import rpc as agent_rpc
from neutron.api.rpc.callbacks import resources
from neutron.common import config as common_config
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.plugins.common import constants as p_constants
from neutron.plugins.ml2.drivers.agent import config as agent_config  # noqa

from networking_lagopus.agent import lagopus_lib
from networking_lagopus.common import config  # noqa


LOG = logging.getLogger(__name__)

LAGOPUS_AGENT_BINARY = 'neutron-lagopus-agent'
AGENT_TYPE_LAGOPUS = 'Lagopus agent'
MAX_WAIT_LAGOPUS_RETRY = 5


class LagopusCache(dict):

    def __init__(self, cls):
        self.cls = cls

    def add(self, name, *args):
        obj = self.cls(name, *args)
        self[name] = obj
        return obj

    def create(self, name, *args):
        if name not in self:
            obj = self.cls(name, *args)
            obj.create()
            self[name] = obj
        return self[name]

    def destroy(self, name):
        if name in self:
            obj = self[name]
            obj.destroy()
            del self[name]

    def show(self):
        return self.cls.show()


@profiler.trace_cls("rpc")
class LagopusManager(object):

    def __init__(self, ryu_app, bridge_mappings):
        self.bridge_mappings = bridge_mappings
        self.ryu_app = ryu_app
        self.serializer = eventlet.semaphore.Semaphore()

        self._wait_lagopus_initialized()

        # initialize device caches
        self.channels = LagopusCache(lagopus_lib.LagopusChannel)
        self.controllers = LagopusCache(lagopus_lib.LagopusController)
        self.interfaces = LagopusCache(lagopus_lib.LagopusInterface)
        self.ports = LagopusCache(lagopus_lib.LagopusPort)
        self.bridges = LagopusCache(lagopus_lib.LagopusBridge)

        # channel
        raw_data = self.channels.show()
        LOG.debug("channels: %s", raw_data)
        for item in raw_data:
            self.channels.add(item["name"])

        # controller
        raw_data = self.controllers.show()
        LOG.debug("controllers: %s", raw_data)
        for item in raw_data:
            channel = item["channel"][1:]  # remove ":"
            self.controllers.add(item["name"], channel)

        # interface
        raw_data = self.interfaces.show()
        LOG.debug("interfaces: %s", raw_data)
        self.num_vhost = 0
        max_pipe_num = 0
        for item in raw_data:
            interface = self.interfaces.add(item["name"], item["type"],
                                            item["device"])
            if interface.type == lagopus_lib.INTERFACE_TYPE_VHOST:
                self.num_vhost += 1
            elif interface.type == lagopus_lib.INTERFACE_TYPE_PIPE:
                max_pipe_num = max(max_pipe_num, interface.id)
        self.next_pipe_id = ((max_pipe_num + 1) / 2) * 2

        # port
        raw_data = self.ports.show()
        LOG.debug("ports: %s", raw_data)
        self.used_vhost_id = []
        for item in raw_data:
            interface = self.interfaces[item["interface"]]
            self.ports.add(item["name"], interface)
            if interface.type == lagopus_lib.INTERFACE_TYPE_VHOST:
                self.used_vhost_id.append(interface.id)

        # bridge
        raw_data = self.bridges.show()
        LOG.debug("bridges: %s", raw_data)
        phys_bridge_names = bridge_mappings.values()
        for item in raw_data:
            b_name = item["name"]
            controller = item["controllers"][0][1:]  # remove ":"
            b_type = (lagopus_lib.BRIDGE_TYPE_PHYS
                      if b_name in phys_bridge_names
                      else lagopus_lib.BRIDGE_TYPE_VLAN)
            bridge = self.bridges.add(b_name, ryu_app, controller,
                                      item["dpid"], b_type, True)
            for p_name, ofport in item["ports"].items():
                port = self.ports[p_name[1:]]  # remove ":"
                bridge.add_port(port, ofport)

        self.phys_to_bridge = {}
        for phys_net, name in bridge_mappings.items():
            if name not in self.bridges:
                LOG.error("Bridge %s not found.", name)
                sys.exit(1)
            self.phys_to_bridge[phys_net] = self.bridges[name]

        LOG.debug("num_vhost: %d, used_vhost_id: %s", self.num_vhost,
                  self.used_vhost_id)

    def _wait_lagopus_initialized(self):
        for retry in range(MAX_WAIT_LAGOPUS_RETRY):
            try:
                lagopus_lib.LagopusChannel.show()
                return
            except socket.error:
                LOG.debug("Lagopus may not be initialized. waiting")
            eventlet.sleep(10)
        LOG.error("Lagopus isn't running")
        sys.exit(1)

    def _channel_name(self, b_name):
        return "ch-%s" % b_name

    def _controller_name(self, b_name):
        return "con-%s" % b_name

    def _sock_path(self, vhost_id):
        return "/tmp/sock%d" % vhost_id

    def _vhost_interface_name(self, vhost_id):
        return "vhost_%d" % vhost_id

    def _rawsock_interface_name(self, device):
        return 'i' + device

    def _rawsock_port_name(self, device):
        return 'p' + device

    def _vlan_bridge_name(self, phys_net, vlan_id):
        return "%s_%d" % (phys_net, vlan_id)

    def _pipe_interface_name(self, pipe_id):
        return "pipe-%d" % pipe_id, "pipe-%d" % (pipe_id + 1)

    def _pipe_device(self, pipe_id):
        device1 = "eth_pipe%d" % pipe_id
        device2 = "eth_pipe%d,attach=%s" % (pipe_id + 1, device1)
        return device1, device2

    def _pipe_port_name(self, pipe_id):
        i_name1, i_name2 = self._pipe_interface_name(pipe_id)
        return "p-%s" % i_name1, "p-%s" % i_name2

    def _get_pipe_id(self):
        pipe_id = self.next_pipe_id
        self.next_pipe_id += 2
        return pipe_id

    def create_pipe_ports(self, bridge):
        if bridge.pipe_id is not None:
            pipe_id = bridge.pipe_id
        else:
            pipe_id = self._get_pipe_id()

        i_name1, i_name2 = self._pipe_interface_name(pipe_id)
        device1, device2 = self._pipe_device(pipe_id)
        inter1 = self.interfaces.create(i_name1, "ethernet-dpdk-phy", device1)
        inter2 = self.interfaces.create(i_name2, "ethernet-dpdk-phy", device2)

        p_name1, p_name2 = self._pipe_port_name(pipe_id)
        port1 = self.ports.create(p_name1, inter1)
        port2 = self.ports.create(p_name2, inter2)

        return port1, port2

    def create_bridge(self, b_name, dpid):
        channel = self._channel_name(b_name)
        self.channels.create(channel)
        controller = self._controller_name(b_name)
        self.controllers.create(controller, channel)
        bridge = self.bridges.create(b_name, self.ryu_app, controller, dpid)
        return bridge

    def bridge_add_port(self, bridge, port):
        if port.bridge is None:
            ofport = bridge.get_ofport()
            bridge.bridge_add_port(port, ofport)

    def bridge_del_port(self, port):
        if port and port.bridge:
            bridge = port.bridge
            bridge.bridge_del_port(port)

    def get_bridge(self, segment):
        phys_net = segment['physical_network']
        phys_bridge = self.phys_to_bridge.get(phys_net)
        if phys_bridge is None:
            # basically this can't be happen since neutron-server
            # already checked before issuing RPC.
            raise ValueError("%s is not configured." % phys_net)

        if (segment['network_type'] == p_constants.TYPE_FLAT):
            return phys_bridge

        vlan_id = segment['segmentation_id']
        b_name = self._vlan_bridge_name(phys_net, vlan_id)
        bridge = self.bridges.get(b_name)
        if bridge is None:
            # vlan bridge does not exeist. so create the bridge
            dpid = (vlan_id << 48) | phys_bridge.dpid
            bridge = self.create_bridge(b_name, dpid)

        # make sure there is pipe connection between phys_bridge
        port1, port2 = self.create_pipe_ports(bridge)
        self.bridge_add_port(bridge, port1)
        self.bridge_add_port(phys_bridge, port2)

        phys_bridge.install_vlan(vlan_id, port2)

        return bridge

    def create_vhost_interface(self, vhost_id):
        sock_path = self._sock_path(vhost_id)
        device = "eth_vhost%d,iface=%s" % (vhost_id, sock_path)
        i_name = self._vhost_interface_name(vhost_id)
        self.interfaces.create(i_name, "ethernet-dpdk-phy", device)
        LOG.debug("vhost %d added.", vhost_id)
        os.system("sudo chmod 777 %s" % sock_path)

    def get_vhost_id(self):
        if self.num_vhost == len(self.used_vhost_id):
            # create new vhost interface
            vhost_id = self.num_vhost
            self.create_vhost_interface(vhost_id)
            self.num_vhost += 1
        else:
            for vhost_id in range(self.num_vhost):
                if vhost_id not in self.used_vhost_id:
                    break
        self.used_vhost_id.append(vhost_id)
        return vhost_id

    def create_vhost_port(self, p_name):
        if p_name not in self.ports:
            vhost_id = self.get_vhost_id()
            i_name = self._vhost_interface_name(vhost_id)
            self.ports.create(p_name, self.interfaces[i_name])
        return self.ports[p_name]

    @log_helpers.log_method_call
    def plug_vhost(self, context, **kwargs):
        # port name == port_id for vhostuser
        p_name = kwargs['port_id']
        segment = kwargs['segment']

        with self.serializer:
            port = self.create_vhost_port(p_name)
            bridge = self.get_bridge(segment)
            self.bridge_add_port(bridge, port)

            return self._sock_path(port.interface.id)

    @log_helpers.log_method_call
    def unplug_vhost(self, context, **kwargs):
        p_name = kwargs['port_id']

        with self.serializer:
            port = self.ports.get(p_name)
            if port:
                self.bridge_del_port(port)
                vhost_id = port.interface.id
                self.ports.destroy(p_name)
                self.used_vhost_id.remove(vhost_id)

    @log_helpers.log_method_call
    def plug_rawsock(self, context, **kwargs):
        device = kwargs['device']
        segment = kwargs['segment']
        i_name = self._rawsock_interface_name(device)
        p_name = self._rawsock_port_name(device)

        with self.serializer:
            if not ip_lib.device_exists(device):
                raise RuntimeError("interface %s does not exist.", device)
            interface = self.interfaces.create(i_name, "ethernet-rawsock",
                                               device)
            port = self.ports.create(p_name, interface)
            bridge = self.get_bridge(segment)
            self.bridge_add_port(bridge, port)

    @log_helpers.log_method_call
    def unplug_rawsock(self, context, **kwargs):
        device = kwargs['device']
        i_name = self._rawsock_interface_name(device)
        p_name = self._rawsock_port_name(device)

        with self.serializer:
            self.bridge_del_port(self.ports.get(p_name))
            self.ports.destroy(p_name)
            self.interfaces.destroy(i_name)


class LagopusAgent(service.Service):

    def __init__(self, ryu_app, bridge_mappings, report_interval,
                 quitting_rpc_timeout, agent_type, agent_binary):
        super(LagopusAgent, self).__init__()
        self.ryu_app = ryu_app
        self.bridge_mappings = bridge_mappings
        self.report_interval = report_interval
        self.quitting_rpc_timeout = quitting_rpc_timeout
        self.agent_type = agent_type
        self.agent_binary = agent_binary
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.REPORTS)

    def start(self):
        self.context = context.get_admin_context_without_session()
        self.manager = LagopusManager(self.ryu_app, self.bridge_mappings)
        self.connection = n_rpc.create_connection()
        self.connection.create_consumer("q-lagopus", [self.manager])

        configurations = {'bridge_mappings': self.bridge_mappings}

        self.agent_state = {
            'binary': self.agent_binary,
            'host': cfg.CONF.host,
            'topic': "q-lagopus",
            'configurations': configurations,
            'agent_type': self.agent_type,
            'resource_versions': resources.LOCAL_RESOURCE_VERSIONS,
            'start_flag': True}

        if self.report_interval:
            heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            heartbeat.start(interval=self.report_interval)

        LOG.info("Agent initialized successfully, now running... ")

        self.connection.consume_in_threads()

    def _report_state(self):
        try:
            devices = len(self.manager.ports)
            self.agent_state['configurations']['devices'] = devices
            self.state_rpc.report_state(self.context, self.agent_state, True)
            # we only want to update resource versions on startup
            self.agent_state.pop('resource_versions', None)
            self.agent_state.pop('start_flag', None)
        except Exception:
            LOG.exception("Failed reporting state!")

    def stop(self, graceful=True):
        LOG.info("Stopping %s agent.", self.agent_type)
        if graceful and self.quitting_rpc_timeout:
            self.state_rpc.client.timeout = self.quitting_rpc_timeout
        super(LagopusAgent, self).stop(graceful)

    def reset(self):
        common_config.setup_logging()


def parse_bridge_mappings():
    try:
        bridge_mappings = helpers.parse_mappings(
            cfg.CONF.lagopus.bridge_mappings)
        LOG.info("Bridge mappings: %s", bridge_mappings)
        return bridge_mappings
    except ValueError as e:
        LOG.error("Parsing bridge_mappings failed: %s. "
                  "Agent terminated!", e)
        sys.exit(1)


def main(ryu_app):
    bridge_mappings = parse_bridge_mappings()
    report_interval = cfg.CONF.AGENT.report_interval
    quitting_rpc_timeout = cfg.CONF.AGENT.quitting_rpc_timeout
    agent = LagopusAgent(ryu_app, bridge_mappings, report_interval,
                         quitting_rpc_timeout,
                         AGENT_TYPE_LAGOPUS,
                         LAGOPUS_AGENT_BINARY)
    launcher = service.launch(cfg.CONF, agent)
    launcher.wait()
