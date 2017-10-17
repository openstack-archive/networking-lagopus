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
import sys

from neutron_lib import context
from neutron_lib.utils import helpers
from oslo_config import cfg
from oslo_log import helpers as log_helpers
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_service import service
from osprofiler import profiler
from ryu.app.ofctl import api as ofctl_api

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
OFPP_MAX = 0xffffff00

INTERFACE_TYPE_VHOST = "vhost"
INTERFACE_TYPE_PIPE = "pipe"


class LagopusInterface(object):

    def __init__(self, name, device):
        self.name = name
        self.type = None
        self.id = None
        self._get_type_and_id(device)

    def _get_type_and_id(self, device):
        if device.startswith("eth_vhost"):
            self.type = INTERFACE_TYPE_VHOST
            self.id = int(device.split(',')[0][len("eth_vhost"):])
        elif device.startswith("eth_pipe"):
            self.type = INTERFACE_TYPE_PIPE
            self.id = int(device.split(',')[0][len("eth_pipe"):])


class LagopusPort(object):

    def __init__(self, name, interface):
        self.name = name
        self.interface = interface
        self.bridge = None


class LagopusBridge(object):

    def __init__(self, ryu_app, name, dpid):
        LOG.debug("LagopusBridge: %s %s", name, dpid)
        self.ryu_app = ryu_app
        self.name = name

        self.max_ofport = 0
        self.used_ofport = []
        self.pipe_ids = []
        self.installed_vlan = []

        self.dpid = dpid
        self.datapath = self._get_datapath()
        self.install_normal()

        self.dump_flows()  # just for debug
        return

    def _get_datapath(self):
        # TODO(hichihara): set timeout
        # NOTE: basically it is OK because lagopus is running
        # and dpid exists at this point. so the call shoud be
        # success.
        while True:
            dp = ofctl_api.get_datapath(self.ryu_app, self.dpid)
            if dp is not None:
                return dp
            # lagopus switch dose not establish connection yet.
            # wait a while
            eventlet.sleep(1)

    def install_normal(self):
        ofp = self.datapath.ofproto
        ofpp = self.datapath.ofproto_parser

        actions = [ofpp.OFPActionOutput(ofp.OFPP_NORMAL, 0)]
        instructions = [ofpp.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS, actions)]
        msg = ofpp.OFPFlowMod(self.datapath,
                              table_id=0,
                              priority=0,
                              instructions=instructions)
        # TODO(hichihara): error handling
        ofctl_api.send_msg(self.ryu_app, msg)

    def install_vlan(self, vlan_id, port):
        if vlan_id in self.installed_vlan:
            return
        ofport = port.ofport
        ofp = self.datapath.ofproto
        ofpp = self.datapath.ofproto_parser

        # pipe port -> phys port: push vlan, output:1
        match = ofpp.OFPMatch(in_port=ofport)
        vlan_vid = vlan_id | ofp.OFPVID_PRESENT
        actions = [ofpp.OFPActionPushVlan(),
                   ofpp.OFPActionSetField(vlan_vid=vlan_vid),
                   ofpp.OFPActionOutput(1, 0)]
        instructions = [ofpp.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS, actions)]
        msg = ofpp.OFPFlowMod(self.datapath,
                              table_id=0,
                              priority=2,
                              match=match,
                              instructions=instructions)
        # TODO(hichihara): error handling
        ofctl_api.send_msg(self.ryu_app, msg)

        # phys port -> pipe port: pop vlan, output:<ofport>
        vlan_vid = vlan_id | ofp.OFPVID_PRESENT
        match = ofpp.OFPMatch(in_port=1, vlan_vid=vlan_vid)
        actions = [ofpp.OFPActionPopVlan(),
                   ofpp.OFPActionOutput(ofport, 0)]
        instructions = [ofpp.OFPInstructionActions(
                        ofp.OFPIT_APPLY_ACTIONS, actions)]
        msg = ofpp.OFPFlowMod(self.datapath,
                              table_id=0,
                              priority=2,
                              match=match,
                              instructions=instructions)
        # TODO(hichihara): error handling
        ofctl_api.send_msg(self.ryu_app, msg)

        self.installed_vlan.append(vlan_id)

    def dump_flows(self):
        ofpp = self.datapath.ofproto_parser
        msg = ofpp.OFPFlowStatsRequest(self.datapath)
        reply_cls = ofpp.OFPFlowStatsReply
        # TODO(hichihara): error handling
        result = ofctl_api.send_msg(self.ryu_app, msg, reply_cls=reply_cls,
                                    reply_multi=True)
        LOG.debug("%s flows: %s", self.name, result)

    def _get_ofport(self):
        if self.max_ofport < OFPP_MAX:
            return self.max_ofport + 1
        else:
            for ofport in range(1, OFPP_MAX + 1):
                if ofport not in self.used_ofport:
                    return ofport

    def _add_port(self, port, ofport):
        self.used_ofport.append(ofport)
        self.max_ofport = max(self.max_ofport, ofport)
        port.ofport = ofport
        port.bridge = self
        if port.interface.type == INTERFACE_TYPE_PIPE:
            self.pipe_ids.append(port.interface.id)

    def add_port(self, port):
        self._add_port(port, self._get_ofport())

    def del_port(self, port):
        self.used_ofport.remove(port.ofport)
        port.bridge = None


@profiler.trace_cls("rpc")
class LagopusManager(object):

    def __init__(self, ryu_app, bridge_mappings):
        self.lagopus_client = lagopus_lib.LagopusCommand()
        self.bridge_mappings = bridge_mappings
        self.ryu_app = ryu_app
        self.serializer = eventlet.semaphore.Semaphore()

        self._wait_lagopus_initialized()

        # initialize device caches
        # channel cache stores name only
        raw_data = self.lagopus_client.show_channels()
        LOG.debug("channels: %s", raw_data)
        self.channels = [item["name"] for item in raw_data]

        # controller cache stores name only
        raw_data = self.lagopus_client.show_controllers()
        LOG.debug("controllers: %s", raw_data)
        self.controllers = [item["name"] for item in raw_data]

        # interface cache
        raw_data = self.lagopus_client.show_interfaces()
        LOG.debug("interfaces: %s", raw_data)
        self.interfaces = {}
        self.num_vhost = 0
        self.num_pipe = 0
        for item in raw_data:
            i_name = item["name"]
            interface = LagopusInterface(i_name, item["device"])
            self.interfaces[i_name] = interface
            if interface.type == INTERFACE_TYPE_VHOST:
                self.num_vhost += 1
            elif interface.type == INTERFACE_TYPE_PIPE:
                self.num_pipe += 1

        # port cache
        raw_data = self.lagopus_client.show_ports()
        LOG.debug("ports: %s", raw_data)
        self.ports = {}
        self.used_vhost_id = []
        for item in raw_data:
            interface = self.interfaces[item["interface"]]
            p_name = item["name"]
            self.ports[p_name] = LagopusPort(p_name, interface)
            if interface.type == INTERFACE_TYPE_VHOST:
                self.used_vhost_id.append(interface.id)

        # bridge cache
        self.bridges = {}
        raw_data = self.lagopus_client.show_bridges()
        LOG.debug("bridges: %s", raw_data)
        for item in raw_data:
            b_name = item["name"]
            bridge = LagopusBridge(ryu_app, b_name, item["dpid"])
            self.bridges[b_name] = bridge
            for p_name, ofport in item["ports"].items():
                port = self.ports[p_name[1:]]  # remove ":"
                bridge._add_port(port, ofport)

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
            data = self.lagopus_client.show_channels()
            if data:
                return
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

    def _create_channel(self, name):
        if name not in self.channels:
            self.lagopus_client.create_channel(name)
            self.channels.append(name)

    def _create_controller(self, name, channel):
        if name not in self.controllers:
            self.lagopus_client.create_controller(name, channel)
            self.controllers.append(name)

    def _create_bridge(self, b_name, controller, dpid):
        if b_name not in self.bridges:
            self.lagopus_client.create_bridge(b_name, controller, dpid)
            self.bridges[b_name] = LagopusBridge(self.ryu_app, b_name, dpid)

    def _create_interface(self, i_name, dev_type, device):
        if i_name not in self.interfaces:
            self.lagopus_client.create_interface(i_name, dev_type, device)
            self.interfaces[i_name] = LagopusInterface(i_name, device)

    def _destroy_interface(self, i_name):
        if i_name in self.interfaces:
            self.lagopus_client.destroy_interface(i_name)
            del self.interfaces[i_name]

    def _create_port(self, p_name, i_name):
        if p_name not in self.ports:
            self.lagopus_client.create_port(p_name, i_name)
            self.ports[p_name] = LagopusPort(p_name, self.interfaces[i_name])

    def _destroy_port(self, p_name):
        if p_name in self.ports:
            self.lagopus_client.destroy_port(p_name)
            del self.ports[p_name]

    def create_pipe_ports(self, bridge):
        if bridge.pipe_ids:
            pipe_id = bridge.pipe_ids[0]
        else:
            pipe_id = self.num_pipe
            self.num_pipe += 2

        i_name1, i_name2 = self._pipe_interface_name(pipe_id)
        device1, device2 = self._pipe_device(pipe_id)
        self._create_interface(i_name1, "ethernet-dpdk-phy", device1)
        self._create_interface(i_name2, "ethernet-dpdk-phy", device2)

        p_name1, p_name2 = self._pipe_port_name(pipe_id)
        self._create_port(p_name1, i_name1)
        self._create_port(p_name2, i_name2)

        return self.ports[p_name1], self.ports[p_name2]

    def create_bridge(self, b_name, dpid):
        channel = self._channel_name(b_name)
        self._create_channel(channel)
        controller = self._controller_name(b_name)
        self._create_controller(controller, channel)
        self._create_bridge(b_name, controller, dpid)
        return self.bridges[b_name]

    def bridge_add_port(self, bridge, port):
        if port.bridge is None:
            bridge.add_port(port)
            self.lagopus_client.bridge_add_port(bridge.name, port.name,
                                                port.ofport)

    def bridge_del_port(self, port):
        if port and port.bridge:
            bridge = port.bridge
            self.lagopus_client.bridge_del_port(bridge.name, port.name)
            bridge.del_port(port)

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
        self._create_interface(i_name, "ethernet-dpdk-phy", device)
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
            self._create_port(p_name, i_name)
        return self.ports[p_name]

    def destroy_vhost_port(self, port):
        if port:
            vhost_id = port.interface.id
            self._destroy_port(port.name)
            self.used_vhost_id.remove(vhost_id)

    def create_rawsock_port(self, device):
        i_name = self._rawsock_interface_name(device)
        p_name = self._rawsock_port_name(device)

        self._create_interface(i_name, "ethernet-rawsock", device)
        self._create_port(p_name, i_name)
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
            self.bridge_del_port(port)
            self.destroy_vhost_port(port)

    @log_helpers.log_method_call
    def plug_rawsock(self, context, **kwargs):
        device = kwargs['device']
        segment = kwargs['segment']

        with self.serializer:
            if not ip_lib.device_exists(device):
                raise RuntimeError("interface %s does not exist.", device)
            port = self.create_rawsock_port(device)
            bridge = self.get_bridge(segment)
            self.bridge_add_port(bridge, port)

    @log_helpers.log_method_call
    def unplug_rawsock(self, context, **kwargs):
        device = kwargs['device']
        i_name = self._rawsock_interface_name(device)
        p_name = self._rawsock_port_name(device)

        with self.serializer:
            self.bridge_del_port(self.ports.get(p_name))
            self._destroy_port(p_name)
            self._destroy_interface(i_name)


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
