channel channel0%(num)d create -dst-addr 127.0.0.1 -protocol tcp
controller controller0%(num)d create -channel channel0%(num)d -role equal -connection-type main
interface interface0%(num)d create -type ethernet-dpdk-phy -port-number %(port)d
port port0%(num)d create -interface interface0%(num)d
bridge bridge0%(num)d create -controller controller0%(num)d -port port0%(num)d 1 -dpid %(num)d -l2-bridge True -mactable-ageing-time 300 -mactable-max-entries 8192
bridge bridge0%(num)d enable
