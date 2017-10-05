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


class LagopusBridge(object):

    def __init__(self, ryu_app, name, dpid, port_data):
        LOG.debug("LagopusBridge: %s %s", name, dpid)
        self.ryu_app = ryu_app
        self.name = name
        self.lagopus_client = lagopus_lib.LagopusCommand()

        self.used_ofport = []
        self.max_ofport = 0
        self.port_mappings = {}

        if port_data:
            for port, ofport in port_data.items():
                # remove ':'
                port_name = port[1:]
                self.port_mappings[port_name] = ofport
                self.used_ofport.append(ofport)
            if self.used_ofport:
                self.max_ofport = max(self.used_ofport)

        LOG.debug("used_ofport: %s, max_ofport: %d",
                  self.used_ofport, self.max_ofport)

        self.dpid = dpid
        self.datapath = self._get_datapath()
        self.install_normal()
        # just for debug
        self.dump_flows()
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

    def install_vlan(self, vlan_id, port_name):
        ofport = self.port_mappings[port_name]
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

    def dump_flows(self):
        ofpp = self.datapath.ofproto_parser
        msg = ofpp.OFPFlowStatsRequest(self.datapath)
        reply_cls = ofpp.OFPFlowStatsReply
        # TODO(hichihara): error handling
        result = ofctl_api.send_msg(self.ryu_app, msg, reply_cls=reply_cls,
                                    reply_multi=True)
        LOG.debug("%s flows: %s", self.name, result)

    def get_ofport(self):
        if self.max_ofport < OFPP_MAX:
            self.max_ofport += 1
            self.used_ofport.append(self.max_ofport)
            return self.max_ofport
        for num in range(1, OFPP_MAX + 1):
            if num not in self.used_ofport:
                self.used_ofport.append(num)
                return num

    def free_ofport(self, ofport):
        if ofport in self.used_ofport:
            self.used_ofport.remove(ofport)

    def add_port(self, port_name):
        b, p = self.lagopus_client.find_bridge_port(port_name, self.name)
        if b is not None:
            LOG.debug("port %s is already pluged.", port_name)
            return

        ofport = self.get_ofport()
        self.port_mappings[port_name] = ofport
        self.lagopus_client.bridge_add_port(self.name, port_name, ofport)


@profiler.trace_cls("rpc")
class LagopusManager(object):

    def __init__(self, ryu_app, bridge_mappings):
        self.lagopus_client = lagopus_lib.LagopusCommand()
        self.bridge_mappings = bridge_mappings
        self.ryu_app = ryu_app

        raw_bridges = self._get_init_bridges()

        self.bridges = {}
        name_to_dpid = {}
        bridge_names = bridge_mappings.values()
        for raw_bridge in raw_bridges:
            name = raw_bridge["name"]
            dpid = raw_bridge["dpid"]
            ports = raw_bridge["ports"]
            self.bridges[dpid] = LagopusBridge(ryu_app, name, dpid, ports)
            if name in bridge_names:
                name_to_dpid[name] = dpid

        self.phys_to_dpid = {}
        for phys_net, name in bridge_mappings.items():
            if name not in name_to_dpid:
                LOG.error("Bridge %s not found.", name)
                sys.exit(1)
            self.phys_to_dpid[phys_net] = name_to_dpid[name]
        LOG.debug("phys_to_dpid: %s", self.phys_to_dpid)

        interfaces = self.lagopus_client.show_interfaces()
        ports = self.lagopus_client.show_ports()
        LOG.debug("interfaces: %s", interfaces)
        LOG.debug("ports: %s", ports)

        # init vhost
        vhost_interfaces = [inter for inter in interfaces
                            if inter["device"].startswith("eth_vhost")]
        self.num_vhost = len(vhost_interfaces)
        used_interfaces = [p["interface"] for p in ports]
        self.used_vhost_id = []
        for inter in vhost_interfaces:
            if inter["name"] in used_interfaces:
                vhost_dev = inter['device'].split(',')[0]
                vhost_id = int(vhost_dev[len("eth_vhost"):])
                self.used_vhost_id.append(vhost_id)
        LOG.debug("num_vhost: %d, used_vhost_id: %s", self.num_vhost,
                  self.used_vhost_id)

        # init pipe
        pipe_interfaces = [inter for inter in interfaces
                           if inter["device"].startswith("eth_pipe")]
        self.num_pipe = len(pipe_interfaces)
        # TODO(hichihara) pipe interface does not remove now.

    def _get_init_bridges(self):
        for retry in range(MAX_WAIT_LAGOPUS_RETRY):
            raw_bridges = self.lagopus_client.show_bridges()
            if raw_bridges:
                LOG.debug("bridges: %s", raw_bridges)
                return raw_bridges
            LOG.debug("Lagopus may not be initialized. waiting")
            eventlet.sleep(10)
        LOG.error("Lagopus isn't running")
        sys.exit(1)

    def get_vhost_interface(self):
        if self.num_vhost == len(self.used_vhost_id):
            # create new vhost interface
            vhost_id = self.num_vhost
            sock_path = "/tmp/sock%d" % vhost_id
            device = "eth_vhost%d,iface=%s" % (vhost_id, sock_path)
            name = "vhost_%d" % vhost_id
            self.lagopus_client.create_vhost_interface(name, device)
            self.num_vhost += 1
            LOG.debug("vhost %d added.", vhost_id)
            os.system("sudo chmod 777 %s" % sock_path)
        else:
            for vhost_id in range(self.num_vhost):
                if vhost_id not in self.used_vhost_id:
                    sock_path = "/tmp/sock%d" % vhost_id
                    name = "vhost_%d" % vhost_id
                    break
        self.used_vhost_id.append(vhost_id)
        return name, sock_path

    def free_vhost_id(self, vhost_id):
        if vhost_id in self.used_vhost_id:
            self.used_vhost_id.remove(vhost_id)

    def port_to_vhost_id(self, port_id):
        ports = self.lagopus_client.show_ports()
        for port in ports:
            if port["name"] == port_id:
                interface = port["interface"]
                if interface.startswith("vhost_"):
                    vhost_id = int(interface[len("vhost_"):])
                    return vhost_id
                return

    def get_pipe(self):
        name0 = "pipe-%d" % self.num_pipe
        name1 = "pipe-%d" % (self.num_pipe + 1)
        device0 = "eth_pipe%d" % self.num_pipe
        device1 = "eth_pipe%d,attach=%s" % (self.num_pipe + 1, device0)
        self.num_pipe += 2

        self.lagopus_client.create_pipe_interface(name0, device0)
        self.lagopus_client.create_pipe_interface(name1, device1)

        return name0, name1

    def get_all_devices(self):
        devices = set()
        ports = self.lagopus_client.show_ports()
        for port in ports:
            devices.add(port["name"])
        LOG.debug("get_all_devices: %s", devices)
        return devices

    def _create_channel(self, channel):
        data = self.lagopus_client.show_channels()
        names = [d['name'] for d in data]
        if channel not in names:
            self.lagopus_client.create_channel(channel)

    def _create_controller(self, controller, channel):
        data = self.lagopus_client.show_controllers()
        names = [d['name'] for d in data]
        if controller not in names:
            self.lagopus_client.create_controller(controller, channel)

    def _create_bridge(self, brname, controller, dpid):
        data = self.lagopus_client.show_bridges()
        names = [d['name'] for d in data]
        if brname not in names:
            self.lagopus_client.create_bridge(brname, controller, dpid)

    def get_bridge(self, segment):
        vlan_id = (segment['segmentation_id']
                   if segment['network_type'] == p_constants.TYPE_VLAN
                   else 0)
        phys_net = segment['physical_network']
        if phys_net not in self.phys_to_dpid:
            # Error
            return
        dpid = (vlan_id << 48) | self.phys_to_dpid[phys_net]
        LOG.debug("vlan_id %d phys dpid %d", vlan_id,
                  self.phys_to_dpid[phys_net])
        LOG.debug("dpid %d 0x%x", dpid, dpid)
        if dpid in self.bridges:
            return self.bridges[dpid]

        # bridge for vlan physical_network does not exist.
        # so create the bridge.
        brname = "%s_%d" % (phys_net, vlan_id)
        channel = "ch-%s" % brname
        self._create_channel(channel)
        controller = "con-%s" % brname
        self._create_controller(controller, channel)
        self._create_bridge(brname, controller, dpid)

        bridge = LagopusBridge(self.ryu_app, brname, dpid, None)
        self.bridges[dpid] = bridge

        pipe1, pipe2 = self.get_pipe()
        port1 = "p-%s" % pipe1
        port2 = "p-%s" % pipe2
        self.lagopus_client.create_port(port1, pipe1)
        self.lagopus_client.create_port(port2, pipe2)

        phys_bridge = self.bridges[self.phys_to_dpid[phys_net]]
        bridge.add_port(port1)
        phys_bridge.add_port(port2)

        phys_bridge.install_vlan(vlan_id, port2)

        return bridge

    @log_helpers.log_method_call
    def plug_vhost(self, context, **kwargs):
        port_id = kwargs['port_id']
        segment = kwargs['segment']

        bridge = self.get_bridge(segment)
        if not bridge:
            # raise
            return

        interface_name, sock_path = self.get_vhost_interface()
        self.lagopus_client.create_port(port_id, interface_name)
        bridge.add_port(port_id)
        return sock_path

    @log_helpers.log_method_call
    def unplug_vhost(self, context, **kwargs):
        port_id = kwargs['port_id']
        bridge_name, _ = self.lagopus_client.find_bridge_port(port_id)
        if not bridge_name:
            LOG.debug("port %s is already unpluged.", port_id)
            return
        self.lagopus_client.bridge_del_port(bridge_name, port_id)
        vhost_id = self.port_to_vhost_id(port_id)
        self.lagopus_client.destroy_port(port_id)
        if vhost_id:
            self.free_vhost_id(vhost_id)

    @log_helpers.log_method_call
    def plug_rawsock(self, context, **kwargs):
        device = kwargs['device']
        segment = kwargs['segment']

        if segment is None:
            LOG.debug("no segment. port may not exist.")
            return

        bridge = self.get_bridge(segment)
        if not bridge:
            return

        interface_name = 'i' + device
        port_name = 'p' + device
        self.lagopus_client.create_rawsock_interface(interface_name, device)
        self.lagopus_client.create_port(port_name, interface_name)
        bridge.add_port(port_name)

        return True

    @log_helpers.log_method_call
    def unplug_rawsock(self, context, **kwargs):
        device = kwargs['device']
        interface_name = 'i' + device
        port_name = 'p' + device

        bridge_name, _ = self.lagopus_client.find_bridge_port(port_name)
        if not bridge_name:
            LOG.debug("device %s is already unpluged.", device)
            return

        self.lagopus_client.bridge_del_port(bridge_name, port_name)
        self.lagopus_client.destroy_port(port_name)
        self.lagopus_client.destroy_interface(interface_name)


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
            devices = len(self.manager.get_all_devices())
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
