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

import logging
import sys

from oslo_config import cfg
from ryu.base import app_manager
from ryu import cfg as ryu_cfg

from neutron.common import config as common_config
from neutron.common import profiler

from networking_lagopus.common import config  # noqa


LAGOPUS_AGENT_BINARY = 'neutron-lagopus-agent'


def init_ryu_config():
    ryu_cfg.CONF(project='ryu', args=[])
    ryu_cfg.CONF.ofp_listen_host = cfg.CONF.lagopus.of_listen_address
    ryu_cfg.CONF.ofp_tcp_listen_port = cfg.CONF.lagopus.of_listen_port

    # enable Debug log for ryu modules
    # why it is necessary see neutron/common/config.py
    for mod in ('ryu.base.app_manager', 'ryu.controller.controller'):
        logger = logging.getLogger(mod)
        logger.setLevel(logging.DEBUG)


def main():
    common_config.init(sys.argv[1:])
    common_config.setup_logging()
    init_ryu_config()
    profiler.setup(LAGOPUS_AGENT_BINARY, cfg.CONF.host)
    app_manager.AppManager.run_apps([
        'networking_lagopus.ml2.agent.lagopus_ryuapp'
    ])
