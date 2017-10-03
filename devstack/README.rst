======================
 Enabling in Devstack
======================

1. Download DevStack

2. Copy the sample local.conf over::

    cp devstack/local.conf.example local.conf

3. Copy the lagopus_agent over::

    cp devstack/lagopus_agent %{DEVSTACK_HOME}/lib/neutron_plugins/
