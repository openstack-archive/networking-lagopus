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

# Save trace setting
_XTRACE_NEUTRON_LAGOPUS=$(set +o | grep xtrace)
set +o xtrace

dir=${GITDIR['networking-lagopus']}

source $dir/devstack/settings
source $dir/devstack/lagopus_agent
source $dir/devstack/lagopus

if is_service_enabled q-agt; then
    if [[ "$1" == "stack" && "$2" == "install" ]]; then
	if [[ "$LAGOPUS_INSTALL" == "True" ]]; then
	    install_lagopus
	fi
	if [[ "$LAGOPUS_RUN" == "True" ]]; then
	    prepare_lagopus
	fi
    elif [[ "$1" == "stack" && "$2" == "post-config" ]]; then
	if [[ "$LAGOPUS_RUN" == "True" ]]; then
	    run_lagopus
	fi
	_neutron_deploy_rootwrap_filters $dir
    elif [[ "$1" == "unstack" ]]; then
	if [[ "$LAGOPUS_RUN" == "True" ]]; then
	    stop_lagopus
            cleanup_lagopus
	fi
    fi
fi

# Restore xtrace
$_XTRACE_NEUTRON_LAGOPUS
