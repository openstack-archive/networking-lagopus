#!/bin/bash

if [ $# -ne 4 ]; then
    echo "Argument Error: ./make_ns.sh veth0 veth1 ns1 172.21.0.1/24 " 1>&2
    exit 1
fi

VETH_01=$1
VETH_02=$2
NAMESPACE=$3
ADDRESS=$4

sudo ip link add $VETH_01 type veth peer name $VETH_02
sudo ip netns add $NAMESPACE
sudo ip link set $VETH_02 netns $NAMESPACE
sudo ip netns exec $NAMESPACE ip addr add $ADDRESS dev $VETH_02
sudo ip link set $VETH_01 up
sudo ip netns exec $NAMESPACE ip link set $VETH_02 up
