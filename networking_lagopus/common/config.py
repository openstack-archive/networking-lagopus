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

from networking_lagopus._i18n import _

DEFAULT_BRIDGE_MAPPINGS = []

lagopus_opts = [
    cfg.BoolOpt('vhost_mode', default=True,
                help=_("Boot virtual machines with vhost_mode ")),
    cfg.ListOpt('bridge_mappings',
                default=DEFAULT_BRIDGE_MAPPINGS,
                help=_("Comma-separated list of "
                       "<physical_network>:<brige> tuples "
                       "mapping physical network names to the agent's "
                       "node-specific physical network interfaces to be used "
                       "for flat and VLAN networks. All physical networks "
                       "listed in network_vlan_ranges on the server should "
                       "have mappings to appropriate interfaces on each "
                       "agent.")),
    cfg.IPOpt('of_listen_address', default='127.0.0.1',
              help=_("Address to listen on for OpenFlow connections. "
                     "Used only for 'native' driver.")),
    cfg.PortOpt('of_listen_port', default=6633,
                help=_("Port to listen on for OpenFlow connections. "
                       "Used only for 'native' driver.")),
]


cfg.CONF.register_opts(lagopus_opts, "lagopus")
