.. _install:

Install
~~~~~~~

This section describes how to install and configure the
networking-lagopus driver, code-named networking_lagopus, on the controller node.

This section assumes that you already have a working OpenStack
environment with at least the following components installed:
.. (add the appropriate services here and further notes)

Install networking-lagopus:

    .. code-block:: console

        $ pip install networking-lagopus

    .. end

Configure networking-lagopus as Neutron ML2 driver into ``/etc/neutron/plugins/ml2/ml2_conf.ini``
file:

    .. path /etc/neutron/plugins/ml2/ml2_conf.ini
    .. code-block:: ini

        [ml2]
        mechanism_drivers = lagopus

    .. end
