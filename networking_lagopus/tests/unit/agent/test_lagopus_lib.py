# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import mock

from neutron.tests import base

from networking_lagopus.agent import lagopus_lib


MOCK_PATCH = ("networking_lagopus.agent.lagopus_lib."
              "LagopusCommand._lagosh")

class TestLagopusLib(base.BaseTestCase):

    def setUp(self):
        super(TestLagopusLib, self).setUp()
        self.lagosh = mock.patch.object(lagopus_lib.LagopusCommand, "_lagosh",
                                        return_value=None).start()
        self.lagopus_client = lagopus_lib.LagopusCommand()

    def test_show_interfaces(self):
        expected_cmd = "interface\n"
        self.lagopus_client.show_interfaces()
        self.lagosh.assert_called_with(expected_cmd)

    def test_show_ports(self):
        expected_cmd = "port\n"
        self.lagopus_client.show_ports()
        self.lagosh.assert_called_with(expected_cmd)

    def test_show_bridges(self):
        expected_cmd = "bridge\n"
        self.lagopus_client.show_bridges()
        self.lagosh.assert_called_with(expected_cmd)

    def test_show_channels(self):
        expected_cmd = "channel\n"
        self.lagopus_client.show_channels()
        self.lagosh.assert_called_with(expected_cmd)

    def test_create_controller(self):
        name = "test-controller"
        channel = "test-channel"
        expected_cmd = ("controller %s create -channel %s -role equal "
                        "-connection-type main\n") % (name, channel)
        self.lagopus_client.create_controller(name, channel)
        self.lagosh.assert_called_with(expected_cmd)

    def test_create_bridge(self):
        name = "test-bridge"
        controller = "test-controller"
        dpid = 1
        expected_cmd_bridge_create = ("bridge %s create -controller %s "
                                      "-dpid %d -l2-bridge True "
                                      "-mactable-ageing-time 300 "
                                      "-mactable-max-entries "
                                      "8192\n") % (name, controller, dpid)
        expected_cmd_bridge_enable = "bridge %s enable\n" % name
        self.lagopus_client.create_bridge(name, controller, dpid)
        expected_calls = [mock.call(expected_cmd_bridge_create),
                          mock.call(expected_cmd_bridge_enable)]
        self.lagosh.assert_has_calls(expected_calls)

    def test_create_vhost_interface(self):
        name = "test-interface"
        device = "test-device"
        expected_cmd = ("interface %s create -type ethernet-dpdk-phy "
                        "-device %s\n") % (name, device)
        self.lagopus_client.create_vhost_interface(name, device)
        self.lagosh.assert_called_with(expected_cmd)

    def test_create_pipe_interface(self):
        name = "test-interface"
        device = "test-device"
        expected_cmd = ("interface %s create -type ethernet-dpdk-phy "
                        "-device %s\n") % (name, device)
        self.lagopus_client.create_pipe_interface(name, device)
        self.lagosh.assert_called_with(expected_cmd)

    def test_create_rawsock_interface(self):
        name = "test-interface"
        device = "test-device"
        expected_cmd = ("interface %s create -type ethernet-rawsock "
                        "-device %s\n") % (name, device)
        self.lagopus_client.create_rawsock_interface(name, device)
        self.lagosh.assert_called_with(expected_cmd)

    def test_create_port(self):
        port = "test-port"
        interface = "test-interface"
        expected_cmd = "port %s create -interface %s\n" % (port, interface)
        self.lagopus_client.create_port(port, interface)
        self.lagosh.assert_called_with(expected_cmd)

    def test_destroy_port(self):
        port = "test-port"
        expected_cmd = "port %s destroy\n" % port
        self.lagopus_client.destroy_port(port)
        self.lagosh.assert_called_with(expected_cmd)

    def test_destroy_interface(self):
        interface = "test-interface"
        expected_cmd = "interface %s destroy\n" % interface
        self.lagopus_client.destroy_interface(interface)
        self.lagosh.assert_called_with(expected_cmd)

    def test_bridge_add_port(self):
        bridge_name = "test-bridge"
        port_name = "test-port"
        ofport = 1
        expected_cmd = ("bridge %s config -port %s %d\n" %
                        (bridge_name, port_name, ofport))
        self.lagopus_client.bridge_add_port(bridge_name,
                                            port_name,
                                            ofport)
        self.lagosh.assert_called_with(expected_cmd)

    def test_bridge_del_port(self):
        bridge_name = "test-bridge"
        port_name = "test-port"
        expected_cmd = ("bridge %s config -port -%s\n" %
                        (bridge_name, port_name))
        self.lagopus_client.bridge_del_port(bridge_name,
                                            port_name)
        self.lagosh.assert_called_with(expected_cmd)
