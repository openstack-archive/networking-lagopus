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

from neutron_lib import constants as n_const
from neutron_lib import context
from oslo_log import log as logging

from neutron.agent.linux import interface as n_interface
from neutron.agent.linux import ip_lib
from neutron.agent import rpc as agent_rpc
from neutron.common import topics

from networking_lagopus.agent import rpc as lagopus_rpc

LOG = logging.getLogger(__name__)


class LagopusInterfaceDriver(n_interface.LinuxInterfaceDriver):

    DEV_NAME_PREFIX = 'ns-'

    def __init__(self, conf):
        super(LagopusInterfaceDriver, self).__init__(conf)
        self.context = context.get_admin_context_without_session()
        self.host = self.conf.host
        self.agent_id = 'lagopus-agent-%s' % self.host
        self.plugin_api = agent_rpc.PluginApi(topic=topics.PLUGIN)
        self.lagopus_api = lagopus_rpc.LagopusAgentApi()

    def _get_network_segment(self, port_id):
        details = self.plugin_api.get_device_details(self.context,
                                                     port_id,
                                                     self.agent_id,
                                                     self.host)
        if details.get('physical_network'):
            return {'physical_network': details['physical_network'],
                    'network_type': details['network_type'],
                    'segmentation_id': details['segmentation_id']}

        raise RuntimeError("Failed to get segment for port %s" % port_id)

    def _disable_tcp_offload(self, namespace, device_name):
        ip_wrapper = ip_lib.IPWrapper(namespace)
        cmd = ['ethtool', '-K', device_name, 'tx', 'off', 'tso', 'off']
        ip_wrapper.netns.execute(cmd)

    def plug(self, network_id, port_id, device_name, mac_address,
             bridge=None, namespace=None, prefix=None, mtu=None):
        # override this method because there are some tasks to be done
        # regardless of whether the interface exists.
        # note that plug_new must be implemented because it is
        # an abstractmethod.
        self.plug_new(network_id, port_id, device_name, mac_address,
                      bridge, namespace, prefix, mtu)

    def plug_new(self, network_id, port_id, device_name, mac_address,
                 bridge=None, namespace=None, prefix=None, mtu=None):
        """Plugin the interface."""
        ip = ip_lib.IPWrapper()
        tap_name = device_name.replace(prefix or self.DEV_NAME_PREFIX,
                                       n_const.TAP_DEVICE_PREFIX)
        if ip_lib.device_exists(device_name, namespace=namespace):
            LOG.info("Device %s already exists", device_name)
            root_veth, ns_veth = n_interface._get_veth(tap_name, device_name,
                                                       namespace)
        else:
            root_veth, ns_veth = ip.add_veth(tap_name, device_name,
                                             namespace2=namespace)
        root_veth.disable_ipv6()
        ns_veth.link.set_address(mac_address)

        if mtu:
            root_veth.link.set_mtu(mtu)
            ns_veth.link.set_mtu(mtu)
        else:
            LOG.warning("No MTU configured for port %s", port_id)

        root_veth.link.set_up()
        ns_veth.link.set_up()
        self._disable_tcp_offload(namespace, device_name)

        segment = self._get_network_segment(port_id)
        self.lagopus_api.plug_rawsock(self.context, tap_name, segment)
        try:
            self.plugin_api.update_device_up(self.context, port_id,
                                             self.agent_id, self.host)
        except RuntimeError as e:
            # the error is not critical. continue.
            LOG.warning("Failed to update_device_up: %s", e)

    def unplug(self, device_name, bridge=None, namespace=None, prefix=None):
        """Unplug the interface."""
        device = ip_lib.IPDevice(device_name, namespace=namespace)
        tap_name = device_name.replace(prefix or self.DEV_NAME_PREFIX,
                                       n_const.TAP_DEVICE_PREFIX)
        try:
            device.link.delete()
        except RuntimeError:
            # note that the interface may not exist.
            LOG.error("Failed deleting interface '%s'", device_name)
        try:
            self.lagopus_api.unplug_rawsock(self.context, tap_name)
            LOG.debug("Unplugged interface '%s'", device_name)
        except RuntimeError:
            LOG.error("Failed unplugging interface '%s'",
                      device_name)
