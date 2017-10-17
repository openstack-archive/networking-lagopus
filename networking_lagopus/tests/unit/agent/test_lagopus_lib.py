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

# TODO(oda): delete unit tests temporarily since the code is heavily refacored.
# make unit tests later.


class TestLagopusLib(base.BaseTestCase):

    def setUp(self):
        super(TestLagopusLib, self).setUp()
        self.lagosh = mock.patch.object(lagopus_lib.LagopusResource, "_exec",
                                        return_value=None).start()
        self.lagopus_resource = lagopus_lib.LagopusResource()

    def test_something(self):
        pass
