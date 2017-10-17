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
from oslo_utils import uuidutils
from ryu.app.ofctl import api as ofctl_api

from neutron.tests import base

from networking_lagopus.agent import lagopus_lib
from networking_lagopus.agent import lagosh

# TODO(oda): delete unit tests temporarily since the code is heavily refacored.
# make unit tests later.


class TestLagopusResource(base.BaseTestCase):

    class FakeResource(lagopus_lib.LagopusResource):
        resource = "test"

    def setUp(self):
        super(TestLagopusResource, self).setUp()
        self.lagosh = mock.patch.object(lagosh.ds_client, "call",
                                        return_value=None).start()
        self.test_resource = self.FakeResource("test_resource")

    def test_create_param_str(self):
        self.assertEqual("", self.test_resource.create_param_str())

    def test_create_str(self):
        self.assertEqual("test test_resource create\n",
                         self.test_resource.create_str())

    def test__exec(self):
        cmd = "test test_resource create\n"
        self.test_resource._exec(cmd)
        self.lagosh.assert_called_with(cmd)

    def test_create(self):
        expected_cmd = "test test_resource create\n"
        self.test_resource.create()
        self.lagosh.assert_called_with(expected_cmd)

    def test_destroy(self):
        expected_cmd = "test test_resource destroy\n"
        self.test_resource.destroy()
        self.lagosh.assert_called_with(expected_cmd)

    def test_show(self):
        expected_cmd = "test\n"
        self.FakeResource.show()
        self.lagosh.assert_called_with(expected_cmd)

    def test_mk_name(self):
        self.assertEqual("unknown", self.FakeResource.mk_name())


class TestLagopusChannel(base.BaseTestCase):

    def setUp(self):
        super(TestLagopusChannel, self).setUp()
        self.lagosh = mock.patch.object(lagosh.ds_client, "call",
                                        return_value=None).start()
        self.test_controller = lagopus_lib.LagopusController("controller",
                                                             "channel")

    def test_create_param_str(self):
        expected_result = "-channel channel -role equal -connection-type main"
        self.assertEqual(expected_result,
                         self.test_controller.create_param_str())

    def test_mk_name(self):
        bridge = "test-bridge"
        expected_result = "con-%s" % bridge
        result = lagopus_lib.LagopusController("controller",
                                               "channel").mk_name(bridge)
        self.assertEqual(expected_result, result)


class TestLagopusInterface(base.BaseTestCase):

    def setUp(self):
        super(TestLagopusInterface, self).setUp()
        self.lagosh = mock.patch.object(lagosh.ds_client, "call",
                                        return_value=None).start()
        name = "interface"
        self.dev_type = lagopus_lib.DEVICE_TYPE_PHYS
        self.device = "eth_vhost1,iface=/tmp/sock1"
        self.test_interface = lagopus_lib.LagopusInterface(name, self.dev_type,
                                                           self.device)

    def test__get_interface_type(self):
        # vhost
        self.assertEqual(lagopus_lib.INTERFACE_TYPE_VHOST,
                         self.test_interface._get_interface_type())

        # pipe
        self.test_interface.device = "eth_pipe1"
        self.assertEqual(lagopus_lib.INTERFACE_TYPE_PIPE,
                         self.test_interface._get_interface_type())

        # Physical Interface
        self.test_interface.device = ""
        self.assertEqual(lagopus_lib.INTERFACE_TYPE_PHYS,
                         self.test_interface._get_interface_type())

        # raw socket
        self.test_interface.dev_type = lagopus_lib.DEVICE_TYPE_RAWSOCK
        self.assertEqual(lagopus_lib.INTERFACE_TYPE_RAWSOCK,
                         self.test_interface._get_interface_type())

    def test__get_id_for_type(self):
        # vhost
        self.assertEqual(1, self.test_interface._get_id_for_type())

        # pipe
        self.test_interface.type = lagopus_lib.INTERFACE_TYPE_PIPE
        self.test_interface.device = "eth_pipe2,attach=eth_pipe1"
        self.assertEqual(2, self.test_interface._get_id_for_type())

    def test_create_param_str(self):
        # vhost
        expected_result = "-type %s -device %s" % (self.dev_type, self.device)
        self.assertEqual(expected_result,
                         self.test_interface.create_param_str())

        # Physical Interface
        self.test_interface.type = lagopus_lib.INTERFACE_TYPE_PHYS
        expected_result = "-type %s -port-number 0" % self.dev_type
        self.assertEqual(expected_result,
                         self.test_interface.create_param_str())

    def test_mk_name(self):
        # vhost
        expected_result = "vhost_1"
        self.assertEqual(
            expected_result,
            self.test_interface.mk_name(lagopus_lib.INTERFACE_TYPE_VHOST, 1))

        # pipe
        expected_result = "pipe-1"
        self.assertEqual(
            expected_result,
            self.test_interface.mk_name(lagopus_lib.INTERFACE_TYPE_PIPE, 1))

        # raw socket
        expected_result = "i1"
        self.assertEqual(
            expected_result,
            self.test_interface.mk_name(lagopus_lib.INTERFACE_TYPE_RAWSOCK, 1))


class TestLagopusPort(base.BaseTestCase):

    def setUp(self):
        super(TestLagopusPort, self).setUp()
        self.lagosh = mock.patch.object(lagosh.ds_client, "call",
                                        return_value=None).start()
        name = "port"
        dev_type = lagopus_lib.DEVICE_TYPE_PHYS
        device = "eth_vhost1,iface=/tmp/sock1"
        interface_name = "test-interface"
        self.test_interface = lagopus_lib.LagopusInterface(interface_name,
                                                           dev_type,
                                                           device)
        self.test_port = lagopus_lib.LagopusPort(name, self.test_interface)

    def test_create_param_str(self):
        self.assertEqual("-interface test-interface",
                         self.test_port.create_param_str())

    def test_add_bridge_str(self):
        self.assertIsNone(self.test_port.add_bridge_str())
        self.test_port.bridge = mock.Mock()
        self.test_port.bridge.name = "test-bridge"
        self.test_port.ofport = "1"
        self.assertEqual("bridge test-bridge config -port port 1\n",
                         self.test_port.add_bridge_str())

    def test_create(self):
        self.test_port.interface.is_used = False
        self.test_port.create()
        self.assertTrue(self.test_port.interface.is_used)

    def test_destroy(self):
        self.test_port.interface.is_used = True
        self.test_port.destroy()
        self.assertFalse(self.test_port.interface.is_used)

    def test_mk_name(self):
        # vhost
        port_id = uuidutils.generate_uuid()
        self.assertEqual(
            port_id,
            self.test_port.mk_name(lagopus_lib.INTERFACE_TYPE_VHOST, port_id))

        # pipe
        pipe_interface = "pipe-1"
        self.assertEqual(
            "p-" + pipe_interface,
            self.test_port.mk_name(lagopus_lib.INTERFACE_TYPE_PIPE,
                                   pipe_interface))

        # raw socket
        device = "taptest"
        self.assertEqual(
            "p" + device,
            self.test_port.mk_name(lagopus_lib.INTERFACE_TYPE_RAWSOCK,
                                   device))


class TestLagopusBridge(base.BaseTestCase):

    def setUp(self):
        super(TestLagopusBridge, self).setUp()
        self.lagosh = mock.patch.object(lagosh.ds_client, "call",
                                        return_value=None).start()
        self.ofctl = mock.patch.object(ofctl_api, "send_msg",
                                       return_value=None).start()
        self.ofctl_get_datapath = mock.patch.object(
            ofctl_api,
            "get_datapath",
            return_value=mock.Mock()).start()
        name = "bridge"
        ryu_app = mock.Mock()
        controller = "test-controller"
        dpid = 1
        self.test_bridge = lagopus_lib.LagopusBridge(name, ryu_app,
                                                     controller, dpid,
                                                     is_enabled=True)

    def test_get_ofport(self):
        self.test_bridge.max_ofport = 0
        self.assertEqual(1, self.test_bridge.get_ofport())
        self.test_bridge.max_ofport = lagopus_lib.OFPP_MAX + 1
        self.test_bridge.used_ofport = [1, 3]
        self.assertEqual(2, self.test_bridge.get_ofport())

    def test_add_port(self):
        port = mock.Mock()
        ofport = 11
        self.test_bridge.max_ofport = 10
        port.interface.type = lagopus_lib.INTERFACE_TYPE_PIPE
        port.interface.id = 1
        self.test_bridge.add_port(port, ofport)
        self.assertEqual(ofport, self.test_bridge.max_ofport)
        self.assertEqual(1, self.test_bridge.pipe_id)

    def test_del_port(self):
        port = mock.Mock()
        port.bridge = self.test_bridge
        port.ofport = 10
        self.test_bridge.used_ofport = [1, 2, 10]
        self.test_bridge.del_port(port)
        self.assertNotIn(10, self.test_bridge.used_ofport)
        self.assertIsNone(port.bridge)
        self.assertIsNone(port.ofport)

    def test_create_param_str(self):
        self.assertEqual("-controller test-controller -dpid 1 "
                         "-l2-bridge True -mactable-ageing-time 300 "
                         "-mactable-max-entries 8192",
                         self.test_bridge.create_param_str())

    def test_enable_str(self):
        self.assertEqual("bridge bridge enable\n",
                         self.test_bridge.enable_str())

    def test_bridge_add_port(self):
        port = mock.Mock()
        port.name = "port"
        ofport = 1
        expected_cmd = "bridge bridge config -port port 1\n"
        with mock.patch.object(lagopus_lib.LagopusBridge,
                               "add_port") as f:
            self.test_bridge.bridge_add_port(port, ofport)
            self.lagosh.assert_called_with(expected_cmd)
            f.assert_called_with(port, ofport)

    def test_bridge_del_port(self):
        port = mock.Mock()
        port.name = "port"
        expected_cmd = "bridge bridge config -port -port\n"
        with mock.patch.object(lagopus_lib.LagopusBridge,
                               "del_port") as f:
            self.test_bridge.bridge_del_port(port)
            self.lagosh.assert_called_with(expected_cmd)
            f.assert_called_with(port)

    def test_mk_name(self):
        phys_net = "test-physical"
        vlan_id = 1
        self.assertEqual(phys_net + "_" + str(vlan_id),
                         self.test_bridge.mk_name(phys_net,
                                                  vlan_id))
