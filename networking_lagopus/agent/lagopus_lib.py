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

import socket

from neutron_lib import constants as n_const
from oslo_log import helpers as log_helpers
from oslo_log import log as logging

from networking_lagopus.agent import lagosh

LOG = logging.getLogger(__name__)
SOCKET_ISSUE = "Socket connection refused.  Lagopus is not running?\n"


class LagopusCommand(object):

    def _lagosh(self, cmd=None):
        if not cmd:
            return
        lagosh_client = lagosh.ds_client()
        try:
            return lagosh_client.call(cmd)
        except socket.error:
            LOG.debug("_lagosh socket error")
        except lagosh.DSLError as e:
            LOG.debug("_lagosh DSLError cmd: %s, error: %s", cmd, e)

    def show_interfaces(self):
        cmd = "interface\n"
        return self._lagosh(cmd)

    def show_ports(self):
        cmd = "port\n"
        return self._lagosh(cmd)

    def show_bridges(self):
        cmd = "bridge\n"
        return self._lagosh(cmd)

    def show_channels(self):
        cmd = "channel\n"
        return self._lagosh(cmd)

    def show_controllers(self):
        cmd = "controller\n"
        return self._lagosh(cmd)

    @log_helpers.log_method_call
    def create_channel(self, name):
        cmd = "channel %s create -dst-addr 127.0.0.1 -protocol tcp\n" % name
        self._lagosh(cmd)

    @log_helpers.log_method_call
    def create_controller(self, name, channel):
        cmd = ("controller %s create -channel %s -role equal "
               "-connection-type main\n") % (name, channel)
        self._lagosh(cmd)

    @log_helpers.log_method_call
    def create_bridge(self, name, controller, dpid):
        cmd = ("bridge %s create -controller %s -dpid %d "
               "-l2-bridge True -mactable-ageing-time 300 "
               "-mactable-max-entries 8192\n") % (name, controller, dpid)
        self._lagosh(cmd)
        cmd = "bridge %s enable\n" % name
        self._lagosh(cmd)

    # TODO(hichihara): unify create_*_interface
    @log_helpers.log_method_call
    def create_vhost_interface(self, name, device):
        cmd = ("interface %s create -type ethernet-dpdk-phy "
               "-device %s\n") % (name, device)
        self._lagosh(cmd)

    def create_pipe_interface(self, name, device):
        self.create_vhost_interface(name, device)

    @log_helpers.log_method_call
    def create_rawsock_interface(self, name, device):
        cmd = ("interface %s create -type ethernet-rawsock "
               "-device %s\n") % (name, device)
        self._lagosh(cmd)

    @log_helpers.log_method_call
    def create_port(self, port, interface):
        cmd = "port %s create -interface %s\n" % (port, interface)
        self._lagosh(cmd)

    @log_helpers.log_method_call
    def destroy_port(self, port):
        cmd = "port %s destroy\n" % port
        self._lagosh(cmd)

    @log_helpers.log_method_call
    def destroy_interface(self, interface):
        cmd = "interface %s destroy\n" % interface
        self._lagosh(cmd)

    @log_helpers.log_method_call
    def bridge_add_port(self, bridge_name, port_name, ofport):
        cmd = ("bridge %s config -port %s %s\n" %
               (bridge_name, port_name, ofport))
        self._lagosh(cmd)

    @log_helpers.log_method_call
    def bridge_del_port(self, bridge_name, port_name):
        cmd = "bridge %s config -port -%s\n" % (bridge_name, port_name)
        self._lagosh(cmd)

    def find_bridge_port(self, port_id, bridge_name=None):
        if port_id.startswith(n_const.TAP_DEVICE_PREFIX):
            port_id = port_id[len(n_const.TAP_DEVICE_PREFIX):]
        bridges = self.show_bridges()
        for bridge in bridges:
            if bridge_name and bridge["name"] != bridge_name:
                continue
            ports = bridge["ports"]
            for port in ports:
                port_name = port[1:]
                if port_name.startswith(port_id):
                    return bridge["name"], port_name
        return None, None
