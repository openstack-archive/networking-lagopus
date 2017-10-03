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

import sys


LAGOPUS_DSL_TEMPLATE = "lagopus_template.dsl"


def main():
    if len(sys.argv) < 3:
        print("usage: generate_dsl dpdk_port_mappings dsl_path")
        return 1
    dpdk_port_mappings = sys.argv[1]
    dsl_path = sys.argv[2]

    phys_nets = []
    for map in dpdk_port_mappings.split(','):
        _, phys = map.split('#')
        phys_nets.append(phys)

    with open(LAGOPUS_DSL_TEMPLATE, "r") as dsl:
        bridge_conf = dsl.read()

    with open(dsl_path, "w") as f:
        num = 1
        for phys in phys_nets:
            f.write(bridge_conf % {'num': num, 'port': num - 1})
            num += 1


if __name__ == "__main__":
    sys.exit(main())
