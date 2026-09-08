package network

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestDefaultRouteIface_ParsesTheDevField(t *testing.T) {
	runner := newFakeRunner()
	runner.stdout["ip -4 route show default"] = "default via 172.17.0.1 dev eth0\n"

	iface, err := defaultRouteIface(context.Background(), runner, "ip")
	require.NoError(t, err)
	assert.Equal(t, "eth0", iface)
}

func TestDefaultRouteIface_ErrorsWithNoDefaultRoute(t *testing.T) {
	runner := newFakeRunner()
	runner.stdout["ip -4 route show default"] = ""

	_, err := defaultRouteIface(context.Background(), runner, "ip")
	require.Error(t, err)
}

func TestIfaceSubnet_ParsesTheNetworkAddressNotTheHostAddress(t *testing.T) {
	runner := newFakeRunner()
	runner.stdout["ip -4 -o addr show dev eth0"] =
		"2: eth0    inet 172.20.0.5/16 brd 172.20.255.255 scope global eth0\\       valid_lft forever preferred_lft forever\n"

	subnet, err := ifaceSubnet(context.Background(), runner, "ip", "eth0")
	require.NoError(t, err)
	assert.Equal(t, "172.20.0.0/16", subnet, "must mask to the network address, not the interface's own host address")
}

func TestIfaceSubnet_ErrorsWithNoIPv4Address(t *testing.T) {
	runner := newFakeRunner()
	runner.stdout["ip -4 -o addr show dev eth0"] = ""

	_, err := ifaceSubnet(context.Background(), runner, "ip", "eth0")
	require.Error(t, err)
}

func TestLooksAlreadyGone(t *testing.T) {
	cases := []struct {
		name   string
		err    error
		expect bool
	}{
		{"missing namespace file", &CommandError{Stderr: "Cannot remove namespace file \"/var/run/netns/x\": No such file or directory"}, true},
		{"missing device", &CommandError{Stderr: "Cannot find device \"veth-h0\""}, true},
		{"generic does not exist", &CommandError{Stderr: "netns x does not exist"}, true},
		{"unrelated failure", &CommandError{Stderr: "Operation not permitted"}, false},
		{"not a CommandError", assertGenericErr(), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.expect, looksAlreadyGone(tc.err))
		})
	}
}

func assertGenericErr() error {
	return context.Canceled
}
