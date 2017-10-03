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

from neutron_lib.api.definitions import portbindings
from neutron_lib import constants
from neutron_lib import context as n_context
from oslo_log import helpers as log_helpers
from oslo_log import log as logging

from neutron.plugins.common import constants as p_constants
from neutron.plugins.ml2 import driver_api as api
from neutron.plugins.ml2.drivers import mech_agent

from networking_lagopus.agent import rpc


LOG = logging.getLogger(__name__)
AGENT_TYPE_LAGOPUS = 'Lagopus agent'


class LagopusMechanismDriver(mech_agent.SimpleAgentMechanismDriverBase):

    @log_helpers.log_method_call
    def __init__(self):
        super(LagopusMechanismDriver, self).__init__(
            AGENT_TYPE_LAGOPUS, portbindings.VIF_TYPE_BRIDGE,
            {portbindings.CAP_PORT_FILTER: False})
        self.context = n_context.get_admin_context_without_session()
        self.lagopus_api = rpc.LagopusAgentApi()

    def get_allowed_network_types(self, agent=None):
        return [p_constants.TYPE_FLAT, p_constants.TYPE_VLAN]

    def get_mappings(self, agent):
        return agent['configurations'].get('bridge_mappings', {})

    def try_to_bind_segment_for_agent(self, context, segment, agent):
        if self.check_segment_for_agent(segment, agent):
            vif_type = self.vif_type
            vif_details = dict(self.vif_details)

            if (context.current['device_owner'].
                    startswith(constants.DEVICE_OWNER_COMPUTE_PREFIX)):
                # use vhostuser for VM
                vif_type = portbindings.VIF_TYPE_VHOST_USER
                vif_details[portbindings.VHOST_USER_MODE] = (
                    portbindings.VHOST_USER_MODE_CLIENT)

                sock_path = self.lagopus_api.plug_vhost(
                    self.context, context.current['id'],
                    segment, context._binding.host)
                vif_details[portbindings.VHOST_USER_SOCKET] = sock_path

            context.set_binding(segment[api.ID], vif_type, vif_details,
                                status=constants.PORT_STATUS_ACTIVE)
            return True
        else:
            return False

    @log_helpers.log_method_call
    def update_port_postcommit(self, context):
        if (context.original_host
                and context.original_vif_type == 'vhostuser'
                and not context.host and context.vif_type == 'unbound'):
            self.lagopus_api.unplug_vhost(self.context,
                                          context.current['id'],
                                          context.original_host)

    @log_helpers.log_method_call
    def delete_port_postcommit(self, context):
        if context.host and context.vif_type == 'vhostuser':
            self.lagopus_api.unplug_vhost(self.context,
                                          context.current['id'],
                                          context.host)
