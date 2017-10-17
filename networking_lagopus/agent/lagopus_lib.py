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

from oslo_log import log as logging
from ryu.app.ofctl import api as ofctl_api

from networking_lagopus.agent import lagosh

LOG = logging.getLogger(__name__)

OFPP_MAX = 0xffffff00

DEVICE_TYPE_PHYS = "ethernet-dpdk-phy"
DEVICE_TYPE_RAWSOCK = "ethernet-rawsock"

INTERFACE_TYPE_VHOST = "vhost"
INTERFACE_TYPE_PIPE = "pipe"
INTERFACE_TYPE_PHYS = "phys"
INTERFACE_TYPE_RAWSOCK = "rawsock"

BRIDGE_TYPE_PHYS = "phys"
BRIDGE_TYPE_VLAN = "vlan"

_config_change_callback = None


def register_config_change_callback(callback):
    global _config_change_callback
    _config_change_callback = callback


def config_changed():
    global _config_change_callback
    if _config_change_callback:
        _config_change_callback()


class LagopusResource(object):

    resource = None

    def __init__(self, name):
        self.name = name

    def create_param_str(self):
        return ""

    def create_str(self):
        cmd = "%s %s create" % (self.resource, self.name)
        param = self.create_param_str()
        if param:
            cmd += " %s\n" % param
        else:
            cmd += "\n"
        return cmd

    def _exec(self, cmd):
        LOG.debug("lagopus cmd executed: %s", cmd.rstrip())
        return lagosh.ds_client().call(cmd)

    def create(self):
        self._exec(self.create_str())

    def destroy(self):
        cmd = "%s %s destroy\n" % (self.resource, self.name)
        self._exec(cmd)

    @classmethod
    def show(cls):
        cmd = "%s\n" % cls.resource
        return lagosh.ds_client().call(cmd)

    @classmethod
    def mk_name(cls):
        return "unknown"


class LagopusChannel(LagopusResource):

    resource = "channel"

    def __init__(self, name):
        super(LagopusChannel, self).__init__(name)

    def create_param_str(self):
        return "-dst-addr 127.0.0.1 -protocol tcp"

    @classmethod
    def mk_name(cls, bridge):
        # channel name convention: "ch-" + bridge name
        return "ch-%s" % bridge


class LagopusController(LagopusResource):

    resource = "controller"

    def __init__(self, name, channel):
        super(LagopusController, self).__init__(name)
        self.channel = channel

    def create_param_str(self):
        return "-channel %s -role equal -connection-type main" % self.channel

    @classmethod
    def mk_name(cls, bridge):
        # controller name convention: "con-" + bridge name
        return "con-%s" % bridge


class LagopusInterface(LagopusResource):

    resource = "interface"

    def __init__(self, name, dev_type, device, port_number=0):
        super(LagopusInterface, self).__init__(name)
        self.dev_type = dev_type
        self.device = device
        self.port_number = port_number
        self.type = self._get_interface_type()
        self.id = self._get_id_for_type()
        self.is_used = False

    def used(self):
        self.is_used = True

    def unused(self):
        self.is_used = False

    def _get_interface_type(self):
        if self.dev_type == DEVICE_TYPE_PHYS:
            if self.device.startswith("eth_vhost"):
                return INTERFACE_TYPE_VHOST
            elif self.device.startswith("eth_pipe"):
                return INTERFACE_TYPE_PIPE
            else:  # device == ""
                return INTERFACE_TYPE_PHYS
        else:  # dev_type == DEVICE_TYPE_RAWSOCK
            return INTERFACE_TYPE_RAWSOCK

    def _get_id_for_type(self):
        if self.type == INTERFACE_TYPE_VHOST:
            return int(self.device.split(',')[0][len("eth_vhost"):])
        elif self.type == INTERFACE_TYPE_PIPE:
            return int(self.device.split(',')[0][len("eth_pipe"):])

    def create_param_str(self):
        type_str = "-type %s " % self.dev_type
        if self.type == INTERFACE_TYPE_PHYS:
            param_str = "-port-number %d" % self.port_number
        else:
            param_str = "-device %s" % self.device
        return type_str + param_str

    @classmethod
    def mk_name(cls, interface_type, name_key):
        # interface name convention:
        #   vhost:         "vhost_" + name_key(==vhost_id)
        #   pipe:          "pipe-" + name_key(==pipe_id)
        #   else(rawsock): "i" + name_key(==device)
        prefix = "i"
        if interface_type == INTERFACE_TYPE_VHOST:
            prefix = "vhost_"
        elif interface_type == INTERFACE_TYPE_PIPE:
            prefix = "pipe-"

        return prefix + str(name_key)


class LagopusPort(LagopusResource):

    resource = "port"

    def __init__(self, name, interface):
        super(LagopusPort, self).__init__(name)
        self.interface = interface
        self.bridge = None

    def create_param_str(self):
        return "-interface %s" % self.interface.name

    def add_bridge_str(self):
        return ("bridge %s config -port %s %s\n" %
                (self.bridge.name, self.name, self.ofport))

    def create(self):
        super(LagopusPort, self).create()
        self.interface.used()

    def destroy(self):
        super(LagopusPort, self).destroy()
        self.interface.unused()

    @classmethod
    def mk_name(cls, interface_type, name_key):
        # port name convention:
        #   vhost:         name_key(==port_id)
        #   pipe:          "p-" + name_key(==pipe interface name)
        #   else(rawsock): "p" + name_key(==device)
        if interface_type == INTERFACE_TYPE_VHOST:
            return name_key
        elif interface_type == INTERFACE_TYPE_PIPE:
            return "p-" + name_key
        else:
            return "p" + name_key


class LagopusBridge(LagopusResource):

    resource = "bridge"

    def __init__(self, name, ryu_app, controller, dpid,
                 b_type=BRIDGE_TYPE_VLAN, is_enabled=False):
        super(LagopusBridge, self).__init__(name)
        self.ryu_app = ryu_app
        self.controller = controller
        self.dpid = dpid
        self.type = b_type
        self.is_enabled = is_enabled

        self.max_ofport = 0
        self.used_ofport = []
        self.pipe_id = None

        if is_enabled:
            self.initialize()

    def create(self):
        super(LagopusBridge, self).create()
        self.enable()

    def initialize(self):
        self.installed_vlan = []
        self.datapath = self._get_datapath()
        self.install_normal()

        self.dump_flows()  # just for debug

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

    def get_ofport(self):
        if self.max_ofport < OFPP_MAX:
            return self.max_ofport + 1
        else:
            for ofport in range(1, OFPP_MAX + 1):
                if ofport not in self.used_ofport:
                    return ofport

    def add_port(self, port, ofport):
        self.used_ofport.append(ofport)
        self.max_ofport = max(self.max_ofport, ofport)
        port.ofport = ofport
        port.bridge = self
        if (self.type == BRIDGE_TYPE_VLAN and
                port.interface.type == INTERFACE_TYPE_PIPE):
            self.pipe_id = port.interface.id

    def del_port(self, port):
        self.used_ofport.remove(port.ofport)
        port.bridge = None

    def create_param_str(self):
        param = ("-controller %s -dpid %d "
                 "-l2-bridge True -mactable-ageing-time 300 "
                 "-mactable-max-entries 8192") % (self.controller,
                                                  self.dpid)
        return param

    def enable_str(self):
        return "bridge %s enable\n" % self.name

    def enable(self):
        self._exec(self.enable_str())
        self.is_enabled = True
        self.initialize()

    def bridge_add_port(self, port, ofport):
        cmd = ("bridge %s config -port %s %s\n" %
               (self.name, port.name, ofport))
        self._exec(cmd)
        self.add_port(port, ofport)
        config_changed()

    def bridge_del_port(self, port):
        cmd = "bridge %s config -port -%s\n" % (self.name, port.name)
        self._exec(cmd)
        self.del_port(port)

    @classmethod
    def mk_name(cls, phys_net, vlan_id):
        # bridge name convention: "phys_net"_"vlan_id"
        # this is used for vlan bridge only.
        return "%s_%d" % (phys_net, vlan_id)
