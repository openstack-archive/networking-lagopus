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

from neutron_lib import constants
from neutron_lib import context
from neutron_lib.utils import helpers
from oslo_config import cfg
from oslo_log import helpers as log_helpers
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_service import service
from osprofiler import profiler

from neutron.agent.common import utils
from neutron.agent.linux import ip_lib
from neutron.agent import rpc as agent_rpc
from neutron.api.rpc.callbacks import resources
from neutron.common import config as common_config
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.plugins.ml2.drivers.agent import config as agent_config  # noqa

from networking_lagopus.agent import lagopus_lib as lg_lib
from networking_lagopus.common import config  # noqa


LOG = logging.getLogger(__name__)

LAGOPUS_AGENT_BINARY = 'neutron-lagopus-agent'
AGENT_TYPE_LAGOPUS = 'Lagopus agent'
MAX_WAIT_LAGOPUS_RETRY = 5

lagopus_dead_handler = None


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
        self.build_cache()
        register_dead_handler(self.lagopus_dead_handler)
        self.lagopus_alive = True

    def build_cache(self):
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
        phys_bridge_names = self.bridge_mappings.values()
        for item in raw_data:
            b_name = item["name"]
            controller = item["controllers"][0][1:]  # remove ":"
            b_type = (lg_lib.BRIDGE_TYPE_PHYS if b_name in phys_bridge_names
                      else lg_lib.BRIDGE_TYPE_VLAN)
            bridge = self.bridges.add(b_name, self.ryu_app, controller,
                                      item["dpid"], b_type, item["is-enabled"])
            for p_name, ofport in item["ports"].items():
                port = self.ports[p_name[1:]]  # remove ":"
                bridge.add_port(port, ofport)

        # check physical bridge existence
        self.phys_to_bridge = {}
        for phys_net, name in self.bridge_mappings.items():
            if name not in self.bridges:
                LOG.error("Bridge %s not found.", name)
                sys.exit(1)
            self.phys_to_bridge[phys_net] = self.bridges[name]

        # vhost and pipe management
        self.max_pipe_pairs = cfg.CONF.lagopus.max_vlan_networks
        self.max_vhosts = (cfg.CONF.lagopus.max_eth_ports
                           - self.max_pipe_pairs * 2
                           - len(self.bridge_mappings))

        self.free_vhost_interfaces = []
        for vhost_id in range(self.max_vhosts):
            interface = self._create_vhost_interface(vhost_id)
            if not interface.is_used:
                self.free_vhost_interfaces.append(interface)

        used_pipe_id = []
        for bridge in self.bridges.values():
            if bridge.pipe_id is not None:
                used_pipe_id.append(bridge.pipe_id)
        self.pipe_pairs = {}
        self.free_pipe_pairs = []
        for i in range(self.max_pipe_pairs):
            pipe_id = i * 2
            pipe_pair = self._create_pipe_pair(pipe_id)
            self.pipe_pairs[pipe_id] = pipe_pair
            if pipe_id not in used_pipe_id:
                self.free_pipe_pairs.append(pipe_pair)

        # install vlan flow
        for bridge in self.bridges.values():
            if (bridge.type == lg_lib.BRIDGE_TYPE_VLAN and
                    bridge.pipe_id is not None):
                port2 = self.pipe_pairs[bridge.pipe_id][1]
                phys_bridge = port2.bridge
                if phys_bridge:
                    vlan_id = bridge.dpid >> 48
                    phys_bridge.install_vlan(vlan_id, port2)

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
        tmp_conf = "/tmp/lagopus-backup.dsl"
        with open(tmp_conf, "w") as f:
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

        if cfg.CONF.lagopus.replace_dsl:
            cmd = ["mv", tmp_conf, cfg.CONF.lagopus.lagopus_conf_path]
            utils.execute(cmd, run_as_root=True)

    def _sock_path(self, vhost_id):
        return "/tmp/sock%d" % vhost_id

    def _create_vhost_interface(self, vhost_id):
        i_name = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_VHOST, vhost_id)
        sock_path = self._sock_path(vhost_id)
        device = "eth_vhost%d,iface=%s,client=1" % (vhost_id, sock_path)
        return self.interfaces.create(i_name, lg_lib.DEVICE_TYPE_PHYS, device)

    def _create_pipe_pair(self, pipe_id):
        i_name1 = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_PIPE, pipe_id)
        i_name2 = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_PIPE,
                                          pipe_id + 1)
        device1 = "eth_pipe%d" % pipe_id
        device2 = "eth_pipe%d,attach=%s" % (pipe_id + 1, device1)

        inter1 = self.interfaces.create(i_name1, lg_lib.DEVICE_TYPE_PHYS,
                                        device1)
        inter2 = self.interfaces.create(i_name2, lg_lib.DEVICE_TYPE_PHYS,
                                        device2)

        p_name1 = self.ports.mk_name(lg_lib.INTERFACE_TYPE_PIPE, i_name1)
        p_name2 = self.ports.mk_name(lg_lib.INTERFACE_TYPE_PIPE, i_name2)
        port1 = self.ports.create(p_name1, inter1)
        port2 = self.ports.create(p_name2, inter2)

        return (port1, port2)

    def get_pipe_ports(self, bridge):
        if bridge.pipe_id is not None:
            return self.pipe_pairs[bridge.pipe_id]
        if self.free_pipe_pairs:
            return self.free_pipe_pairs.pop()
        raise RuntimeError("too many networks.")

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

        if (segment['network_type'] == constants.TYPE_FLAT):
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
        port1, port2 = self.get_pipe_ports(bridge)
        self.bridge_add_port(bridge, port1)
        self.bridge_add_port(phys_bridge, port2)

        phys_bridge.install_vlan(vlan_id, port2)

        return bridge

    def put_bridge(self, bridge):
        if bridge is None or bridge.type != lg_lib.BRIDGE_TYPE_VLAN:
            return
        if len(bridge.used_ofport) > 1:
            return

        port1, port2 = self.pipe_pairs[bridge.pipe_id]
        phys_bridge = port2.bridge

        if phys_bridge:
            vlan_id = bridge.dpid >> 48
            phys_bridge.uninstall_vlan(vlan_id, port2)
        self.bridge_del_port(port1)
        self.bridge_del_port(port2)

        self.bridges.destroy(bridge.name)
        self.free_pipe_pairs.append((port1, port2))

    def create_vhost_port(self, p_name):
        if p_name in self.ports:
            return self.ports[p_name]

        if self.free_vhost_interfaces:
            interface = self.free_vhost_interfaces.pop()
        else:
            raise RuntimeError("too many vhosts.")

        return self.ports.create(p_name, interface)

    def destroy_vhost_port(self, port):
        interface = port.interface
        self.ports.destroy(port.name)
        self.free_vhost_interfaces.append(interface)

    @log_helpers.log_method_call
    def plug_vhost(self, context, **kwargs):
        self.check_active()
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
        self.check_active()
        p_name = self.ports.mk_name(lg_lib.INTERFACE_TYPE_VHOST,
                                    kwargs['port_id'])

        with self.serializer:
            port = self.ports.get(p_name)
            if port:
                bridge = port.bridge
                self.bridge_del_port(port)
                self.destroy_vhost_port(port)
                self.put_bridge(bridge)

    @log_helpers.log_method_call
    def plug_rawsock(self, context, **kwargs):
        self.check_active()
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
        self.check_active()
        device = kwargs['device']
        i_name = self.interfaces.mk_name(lg_lib.INTERFACE_TYPE_RAWSOCK, device)
        p_name = self.ports.mk_name(lg_lib.INTERFACE_TYPE_RAWSOCK, device)

        with self.serializer:
            port = self.ports.get(p_name)
            bridge = port.bridge if port else None
            self.bridge_del_port(port)
            self.ports.destroy(p_name)
            self.interfaces.destroy(i_name)
            self.put_bridge(bridge)

    def lagopus_dead_handler(self, dpid):
        for bridge in self.phys_to_bridge.values():
            if dpid == bridge.dpid:
                if self.lagopus_alive:
                    # call once at first detected
                    self.lagopus_alive = False
                    eventlet.spawn_n(self.wait_lagopus_restart)

    def wait_lagopus_restart(self):
        LOG.warning("Detect lagopus down. wait lagopus restart.")

        while True:
            eventlet.sleep(10)
            try:
                lg_lib.LagopusChannel.show()
                break
            except Exception:
                pass

        LOG.info("lagopus restarted. initialize again...")
        try:
            self.build_cache()
        except Exception:
            LOG.error("Re-initialization failed.")
            eventlet.sleep(0)  # to output log
            # give up
            os._exit(1)

        LOG.info("Re-initialization done. now operate normaly.")
        self.lagopus_alive = True

    def is_active(self):
        return self.lagopus_alive

    def check_active(self):
        if not self.lagopus_alive:
            raise RuntimeError("lagopus is down.")


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
            if not self.manager.is_active():
                return
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


def register_dead_handler(handler):
    global lagopus_dead_handler
    lagopus_dead_handler = handler


def handle_dead(dpid):
    global lagopus_dead_handler
    if lagopus_dead_handler:
        lagopus_dead_handler(dpid)


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
