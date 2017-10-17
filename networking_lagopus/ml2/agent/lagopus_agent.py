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

from networking_lagopus.agent import lagopus_lib as lg_lib
from networking_lagopus.common import config  # noqa


LOG = logging.getLogger(__name__)

LAGOPUS_AGENT_BINARY = 'neutron-lagopus-agent'
AGENT_TYPE_LAGOPUS = 'Lagopus agent'
MAX_WAIT_LAGOPUS_RETRY = 5


class LagopusCache(dict):

    def __init__(self, resource_cls):
        self.resource_cls = resource_cls

    def add(self, name, *args):
        # this method is called when initialization to register already
        # exist resources.
        obj = self.resource_cls(name, *args)
        self[name] = obj
        return obj

    def create(self, name, *args):
        if name not in self:
            obj = self.resource_cls(name, *args)
            obj.create()
            self[name] = obj
            lg_lib.config_changed()
        return self[name]

    def destroy(self, name):
        if name in self:
            obj = self[name]
            obj.destroy()
            del self[name]
            lg_lib.config_changed()

    def show(self):
        return self.resource_cls.show()

    def mk_name(self, *args):
        return self.resource_cls.mk_name(*args)


@profiler.trace_cls("rpc")
class LagopusManager(object):

    def __init__(self, ryu_app, bridge_mappings):
        self.bridge_mappings = bridge_mappings
        self.ryu_app = ryu_app
        self.serializer = eventlet.semaphore.Semaphore()

        self._wait_lagopus_initialized()

        lg_lib.register_config_change_callback(self._rebuild_dsl)

        # initialize device caches
        # channel
        self.channels = LagopusCache(lg_lib.LagopusChannel)
        raw_data = self.channels.show()
        LOG.debug("channels: %s", raw_data)
        for item in raw_data:
            self.channels.add(item["name"])

        # controller
        self.controllers = LagopusCache(lg_lib.LagopusController)
        raw_data = self.controllers.show()
        LOG.debug("controllers: %s", raw_data)
        for item in raw_data:
            self.controllers.add(item["name"], item["channel"])

        # interface
        self.interfaces = LagopusCache(lg_lib.LagopusInterface)
        raw_data = self.interfaces.show()
        LOG.debug("interfaces: %s", raw_data)
        for item in raw_data:
            interface = self.interfaces.add(item["name"], item["type"],
                                            item["device"],
                                            item.get("port-number"))

        # port
        self.ports = LagopusCache(lg_lib.LagopusPort)
        raw_data = self.ports.show()
        LOG.debug("ports: %s", raw_data)
        for item in raw_data:
            interface = self.interfaces[item["interface"]]
            self.ports.add(item["name"], interface)
            interface.used()

        # bridge
        self.bridges = LagopusCache(lg_lib.LagopusBridge)
        raw_data = self.bridges.show()
        LOG.debug("bridges: %s", raw_data)
        phys_bridge_names = bridge_mappings.values()
        for item in raw_data:
            b_name = item["name"]
            controller = item["controllers"][0][1:]  # remove ":"
            b_type = (lg_lib.BRIDGE_TYPE_PHYS if b_name in phys_bridge_names
                      else lg_lib.BRIDGE_TYPE_VLAN)
            bridge = self.bridges.add(b_name, ryu_app, controller,
                                      item["dpid"], b_type, item["is-enabled"])
            for p_name, ofport in item["ports"].items():
                port = self.ports[p_name[1:]]  # remove ":"
                bridge.add_port(port, ofport)

        # check physical bridge existence
        self.phys_to_bridge = {}
        for phys_net, name in bridge_mappings.items():
            if name not in self.bridges:
                LOG.error("Bridge %s not found.", name)
                sys.exit(1)
            self.phys_to_bridge[phys_net] = self.bridges[name]

        # vost_id and pipe_id management
        self.free_vhost_interfaces = []
        self.num_vhost = 0
        max_pipe_num = 0
        for interface in self.interfaces.values():
            if interface.type == lg_lib.INTERFACE_TYPE_VHOST:
                self.num_vhost += 1
                if not interface.is_used:
                    sock_path = self._sock_path(interface.id)
                    os.system("sudo chmod 777 %s" % sock_path)
                    self.free_vhost_interfaces.append(interface)
            elif interface.type == lg_lib.INTERFACE_TYPE_PIPE:
                # only interested in even number
                if interface.id % 2 == 0:
                    max_pipe_num = max(max_pipe_num, interface.id)
        self.next_pipe_id = max_pipe_num + 2

        # make initial dsl
        self._rebuild_dsl()

    def _wait_lagopus_initialized(self):
        for retry in range(MAX_WAIT_LAGOPUS_RETRY):
            try:
                lg_lib.LagopusChannel.show()
                return
            except socket.error:
                LOG.debug("Lagopus may not be initialized. waiting")
            eventlet.sleep(10)
        LOG.error("Lagopus isn't running")
        sys.exit(1)

    def _rebuild_dsl(self):
        # TODO(oda): just for backup now. it is able to restart lagopus
        # uging this dsl manually. replace actual dsl in the future.
        path = "/tmp/lagopus-backup.dsl"  # path is temporary
        with open(path, "w") as f:
            for obj in self.channels.values():
                f.write(obj.create_str())
            for obj in self.controllers.values():
                f.write(obj.create_str())
            for obj in self.bridges.values():
                f.write(obj.create_str())
                f.write(obj.enable_str())
            # make interfaces lexical order. it is intended to make
            # 'pipe-0' in advance of 'pipe-1' for example.
            for name in sorted(self.interfaces.keys()):
                obj = self.interfaces[name]
                f.write(obj.create_str())
            for obj in self.ports.values():
                f.write(obj.create_str())
                if obj.bridge:
                    f.write(obj.add_bridge_str())

    def _sock_path(self, vhost_id):
        return "/tmp/sock%d" % vhost_id

    def create_pipe_interfaces(self, pipe_id):
        i_name1 = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_PIPE, pipe_id)
        i_name2 = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_PIPE,
                                          pipe_id + 1)
        device1 = "eth_pipe%d" % pipe_id
        device2 = "eth_pipe%d,attach=%s" % (pipe_id + 1, device1)

        inter1 = self.interfaces.create(i_name1, lg_lib.DEVICE_TYPE_PHYS,
                                        device1)
        inter2 = self.interfaces.create(i_name2, lg_lib.DEVICE_TYPE_PHYS,
                                        device2)

        return inter1, inter2

    def _get_pipe_id(self):
        pipe_id = self.next_pipe_id
        self.next_pipe_id += 2
        return pipe_id

    def create_pipe_ports(self, bridge):
        if bridge.pipe_id is not None:
            pipe_id = bridge.pipe_id
        else:
            pipe_id = self._get_pipe_id()

        inter1, inter2 = self.create_pipe_interfaces(pipe_id)

        p_name1 = self.ports.mk_name(lg_lib.INTERFACE_TYPE_PIPE, inter1.name)
        p_name2 = self.ports.mk_name(lg_lib.INTERFACE_TYPE_PIPE, inter2.name)
        port1 = self.ports.create(p_name1, inter1)
        port2 = self.ports.create(p_name2, inter2)

        return port1, port2

    def create_bridge(self, b_name, dpid):
        channel = self.channels.mk_name(b_name)
        self.channels.create(channel)
        controller = self.controllers.mk_name(b_name)
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
        b_name = self.bridges.mk_name(phys_net, vlan_id)
        bridge = self.bridges.get(b_name)
        if bridge is None:
            # vlan bridge does not exeist. so create the bridge
            dpid = (vlan_id << 48) | phys_bridge.dpid
            bridge = self.create_bridge(b_name, dpid)
        elif not bridge.is_enabled:
            bridge.enable()

        # make sure there is pipe connection between phys_bridge
        port1, port2 = self.create_pipe_ports(bridge)
        self.bridge_add_port(bridge, port1)
        self.bridge_add_port(phys_bridge, port2)

        phys_bridge.install_vlan(vlan_id, port2)

        return bridge

    def create_vhost_interface(self, vhost_id):
        i_name = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_VHOST, vhost_id)
        sock_path = self._sock_path(vhost_id)
        device = "eth_vhost%d,iface=%s" % (vhost_id, sock_path)
        interface = self.interfaces.create(i_name, lg_lib.DEVICE_TYPE_PHYS,
                                           device)
        LOG.debug("vhost %d added.", vhost_id)
        os.system("sudo chmod 777 %s" % sock_path)
        return interface

    def get_vhost_interface(self):
        if self.free_vhost_interfaces:
            return self.free_vhost_interfaces.pop()

        # create new vhost interface
        vhost_id = self.num_vhost
        interface = self.create_vhost_interface(vhost_id)
        self.num_vhost += 1
        return interface

    def create_vhost_port(self, p_name):
        if p_name not in self.ports:
            interface = self.get_vhost_interface()
            self.ports.create(p_name, interface)
        return self.ports[p_name]

    @log_helpers.log_method_call
    def plug_vhost(self, context, **kwargs):
        p_name = self.ports.mk_name(lg_lib.INTERFACE_TYPE_VHOST,
                                    kwargs['port_id'])
        segment = kwargs['segment']

        with self.serializer:
            port = self.create_vhost_port(p_name)
            bridge = self.get_bridge(segment)
            self.bridge_add_port(bridge, port)

            return self._sock_path(port.interface.id)

    @log_helpers.log_method_call
    def unplug_vhost(self, context, **kwargs):
        p_name = self.ports.mk_name(lg_lib.INTERFACE_TYPE_VHOST,
                                    kwargs['port_id'])

        with self.serializer:
            port = self.ports.get(p_name)
            if port:
                self.bridge_del_port(port)
                interface = port.interface
                self.ports.destroy(p_name)
                self.free_vhost_interfaces.append(interface)

    @log_helpers.log_method_call
    def plug_rawsock(self, context, **kwargs):
        device = kwargs['device']
        segment = kwargs['segment']
        i_name = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_RAWSOCK, device)
        p_name = self.ports.mk_name(lg_lib.INTERFACE_TYPE_RAWSOCK, device)

        with self.serializer:
            if not ip_lib.device_exists(device):
                raise RuntimeError("interface %s does not exist.", device)
            interface = self.interfaces.create(i_name,
                                               lg_lib.DEVICE_TYPE_RAWSOCK,
                                               device)
            port = self.ports.create(p_name, interface)
            bridge = self.get_bridge(segment)
            self.bridge_add_port(bridge, port)

    @log_helpers.log_method_call
    def unplug_rawsock(self, context, **kwargs):
        device = kwargs['device']
        i_name = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_RAWSOCK, device)
        p_name = self.ports.mk_name(lg_lib.INTERFACE_TYPE_RAWSOCK, device)

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
