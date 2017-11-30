======================
 Enabling in Devstack
======================

1. Download DevStack

2. Copy the sample local.conf over::

    $ cp devstack/sample-local.conf local.conf

3. Run stack.sh::

    $ ./stack.sh

4. Edit libvirtd conf and restart::

    $ sudo vi /etc/libvirt/qemu.conf
    ...
    security_driver = "none"
    ...
    $ sudo service libvirtd stop
    $ sudo service libvirtd start
