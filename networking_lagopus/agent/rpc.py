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

from oslo_config import cfg
from oslo_log import log as logging
import oslo_messaging

from neutron.common import rpc as n_rpc


LOG = logging.getLogger(__name__)


class LagopusAgentApi(object):
    '''Lagopus agent RPC API

    API version history:
        1.0 - Initial version.
    '''

    def __init__(self):
        target = oslo_messaging.Target(version='1.0')
        self.client = n_rpc.get_client(target)

    def _get_context(self, host):
        if host is None:
            host = cfg.CONF.host
        topic = "q-lagopus.%s" % host
        return self.client.prepare(topic=topic)

    def plug_rawsock(self, context, device, segment, host=None):
        cctxt = self._get_context(host)
        return cctxt.call(context, 'plug_rawsock',
                          device=device, segment=segment)

    def unplug_rawsock(self, context, device, host=None):
        cctxt = self._get_context(host)
        return cctxt.call(context, 'unplug_rawsock',
                          device=device)

    def plug_vhost(self, context, port_id, segment, host=None):
        cctxt = self._get_context(host)
        return cctxt.call(context, 'plug_vhost',
                          port_id=port_id, segment=segment)

    def unplug_vhost(self, context, port_id, host=None):
        cctxt = self._get_context(host)
        return cctxt.call(context, 'unplug_vhost',
                          port_id=port_id)
