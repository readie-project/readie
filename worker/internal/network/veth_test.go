package network

import (
	"context"
	"fmt"
	"net/netip"
	"sync"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// fakeRunner records every invocation and lets a test fail one by prefix.
type fakeRunner struct {
	mu    sync.Mutex
	calls [][]string
	// failOn maps a joined-args prefix to the error that call should return.
	failOn map[string]error
	// stdout maps a joined-args prefix to canned stdout.
	stdout map[string]string
}

func newFakeRunner() *fakeRunner {
	return &fakeRunner{failOn: map[string]error{}, stdout: map[string]string{}}
}

func (f *fakeRunner) Output(_ context.Context, binary string, args ...string) (Result, error) {
	f.mu.Lock()
	defer f.mu.Unlock()

	full := append([]string{binary}, args...)
	f.calls = append(f.calls, full)

	key := joinArgs(full)
	for prefix, err := range f.failOn {
		if hasPrefix(full, splitArgs(prefix)) {
			delete(f.failOn, prefix) // one-shot: models a transient failure, not a permanently broken command
			return Result{ExitCode: 1, Stderr: "boom"}, err
		}
	}
	return Result{Stdout: f.stdout[key]}, nil
}

func (f *fakeRunner) Calls() [][]string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([][]string(nil), f.calls...)
}

func joinArgs(args []string) string {
	out := ""
	for i, a := range args {
		if i > 0 {
			out += " "
		}
		out += a
	}
	return out
}

func splitArgs(s string) []string {
	var out []string
	cur := ""
	for _, r := range s {
		if r == ' ' {
			if cur != "" {
				out = append(out, cur)
				cur = ""
			}
			continue
		}
		cur += string(r)
	}
	if cur != "" {
		out = append(out, cur)
	}
	return out
}

func hasPrefix(full, prefix []string) bool {
	if len(prefix) > len(full) {
		return false
	}
	for i, p := range prefix {
		if full[i] != p {
			return false
		}
	}
	return true
}

func newTestProvisioner(t *testing.T, runner *fakeRunner) *VethProvisioner {
	t.Helper()
	stubHostIfaceAddr(runner, "eth0", "172.20.0.5/16")
	p, err := New(Options{
		Subnet:    "192.168.200.0/24",
		Capacity:  4,
		HostIface: "eth0", // skip default-route auto-detection in tests
		Runner:    runner,
	})
	require.NoError(t, err)
	// Real forwarding is a direct procfs write, not a Runner call, and does not
	// exist on a test machine's /proc/sys the way this package expects on a
	// privileged worker container; stub it out.
	p.enableIPForward = func() error { return nil }
	require.NoError(t, p.WarmUp(context.Background()))
	return p
}

// stubHostIfaceAddr fakes what `ip -4 -o addr show dev <iface>` prints, so
// setupNAT's ifaceSubnet lookup (used to exclude the host's own network from
// sandbox egress) succeeds against a fake runner.
func stubHostIfaceAddr(runner *fakeRunner, iface, cidr string) {
	runner.stdout["ip -4 -o addr show dev "+iface] =
		fmt.Sprintf("2: %s    inet %s brd 172.20.255.255 scope global %s\n", iface, cidr, iface)
}

func TestVethProvisioner_WarmUpRunsNATSetupOnlyOnce(t *testing.T) {
	runner := newFakeRunner()
	stubHostIfaceAddr(runner, "eth0", "172.20.0.5/16")
	p, err := New(Options{Subnet: "192.168.200.0/24", Capacity: 4, HostIface: "eth0", Runner: runner})
	require.NoError(t, err)
	enableIPForwardCalls := 0
	p.enableIPForward = func() error { enableIPForwardCalls++; return nil }

	require.NoError(t, p.WarmUp(context.Background()))

	assert.Equal(t, 1, enableIPForwardCalls, "NAT/forwarding setup must run once, not once per slot")

	iptablesCalls := 0
	for _, c := range runner.Calls() {
		if len(c) > 0 && c[0] == "iptables" {
			iptablesCalls++
		}
	}
	assert.Equal(t, 5, iptablesCalls, "the 5 shared rules must be installed once total, not once per slot")
}

func TestVethProvisioner_FailsClosedWhenTheHostSubnetCannotBeDetermined(t *testing.T) {
	// Provisioning must never silently fall back to unrestricted egress: if
	// the host's own subnet (to exclude from it) can't be determined, no
	// sandbox network should be stood up at all.
	runner := newFakeRunner()
	// No stubHostIfaceAddr call: "ip -4 -o addr show dev eth0" returns empty,
	// so ifaceSubnet fails.
	p, err := New(Options{Subnet: "192.168.200.0/24", Capacity: 4, HostIface: "eth0", Runner: runner})
	require.NoError(t, err)
	p.enableIPForward = func() error { return nil }

	err = p.WarmUp(context.Background())
	require.Error(t, err)

	for _, c := range runner.Calls() {
		require.NotEqual(t, "netns", firstOrEmpty(c, 1), "no netns should be created when the egress restriction couldn't be set up")
	}
}

func firstOrEmpty(ss []string, i int) string {
	if i < len(ss) {
		return ss[i]
	}
	return ""
}

func TestVethProvisioner_ExcludesTheHostsOwnNetworkAndLinkLocalFromEgress(t *testing.T) {
	runner := newFakeRunner()
	p := newTestProvisioner(t, runner) // stubs eth0 at 172.20.0.5/16

	_, err := p.Provision(context.Background(), "a")
	require.NoError(t, err)

	// The fake's default Output never fails, so setupNAT's check-then-add
	// idempotency logic finds every rule "already present" and only ever
	// issues the -C check, never the -A add - but the check carries the same
	// rule content (source/dest/interface/target) in the same order, which is
	// all this test cares about.
	var forwardRules [][]string
	for _, c := range runner.Calls() {
		if len(c) > 1 && c[0] == "iptables" && (c[1] == "-A" || c[1] == "-C") && contains(c, "FORWARD") {
			forwardRules = append(forwardRules, c)
		}
	}

	var sawHostSubnetDrop, sawLinkLocalDrop bool
	acceptIdx := -1
	for i, rule := range forwardRules {
		switch {
		case ruleTarget(rule) == "DROP" && contains(rule, "172.20.0.0/16"):
			sawHostSubnetDrop = true
		case ruleTarget(rule) == "DROP" && contains(rule, "169.254.0.0/16"):
			sawLinkLocalDrop = true
		case ruleTarget(rule) == "ACCEPT" && contains(rule, "-s") && contains(rule, "-o"):
			acceptIdx = i
		}
	}

	assert.True(t, sawHostSubnetDrop, "the host's own Docker-network subnet must be excluded from sandbox egress")
	assert.True(t, sawLinkLocalDrop, "link-local (incl. the cloud metadata address) must be excluded from sandbox egress")
	require.GreaterOrEqual(t, acceptIdx, 0, "a general egress ACCEPT rule must still exist")

	for i, rule := range forwardRules {
		if ruleTarget(rule) == "DROP" {
			assert.Less(t, i, acceptIdx, "DROP rules must precede the general ACCEPT rule to actually take effect")
		}
	}
}

func ruleTarget(rule []string) string {
	for i, a := range rule {
		if a == "-j" && i+1 < len(rule) {
			return rule[i+1]
		}
	}
	return ""
}

func contains(ss []string, s string) bool {
	for _, v := range ss {
		if v == s {
			return true
		}
	}
	return false
}

func TestVethProvisioner_ProvisionIsIdempotentPerID(t *testing.T) {
	runner := newFakeRunner()
	p := newTestProvisioner(t, runner)

	first, err := p.Provision(context.Background(), "a")
	require.NoError(t, err)
	before := len(runner.Calls())

	second, err := p.Provision(context.Background(), "a")
	require.NoError(t, err)

	assert.Equal(t, first, second)
	assert.Equal(t, before, len(runner.Calls()), "a repeat Provision for a still-live id must not redo any work")
}

func TestVethProvisioner_ProvisionUsesDistinctVethNamesPerSlot(t *testing.T) {
	runner := newFakeRunner()
	p := newTestProvisioner(t, runner)

	allocA, err := p.Provision(context.Background(), "a")
	require.NoError(t, err)
	allocB, err := p.Provision(context.Background(), "b")
	require.NoError(t, err)

	// Veth names live inside the -batch file's content now, not in argv, so
	// this asserts on outcome (distinct addresses per slot) rather than
	// inspecting the temp file directly - see the hostSetupCommands/
	// netnsSetupCommands unit tests below for the batch content itself.
	assert.NotEqual(t, allocA.SandboxIP, allocB.SandboxIP)
	assert.NotEqual(t, allocA.HostIP, allocB.HostIP)
}

func TestHostSetupCommands_UsesTheGivenNamesAndAddress(t *testing.T) {
	hostIP := netip.MustParseAddr("192.168.200.2")
	lines := hostSetupCommands("exec_container-abc", "veth-h0", "veth-s0", hostIP)

	assert.Equal(t, []string{
		"netns add exec_container-abc",
		"link add veth-h0 type veth peer name veth-s0",
		"link set veth-s0 netns exec_container-abc",
		"addr add 192.168.200.2/30 dev veth-h0",
		"link set veth-h0 up",
	}, lines)
}

func TestNetnsSetupCommands_UsesTheGivenNamesAndAddresses(t *testing.T) {
	sandboxIP := netip.MustParseAddr("192.168.200.1")
	hostIP := netip.MustParseAddr("192.168.200.2")
	lines := netnsSetupCommands("veth-s0", sandboxIP, hostIP)

	assert.Equal(t, []string{
		"addr add 192.168.200.1/30 dev veth-s0",
		"link set veth-s0 up",
		"link set lo up",
		"route add default via 192.168.200.2",
	}, lines)
}

func TestVethProvisioner_WarmUpUsesTwoIPInvocationsPerSlotNotNine(t *testing.T) {
	runner := newFakeRunner()
	stubHostIfaceAddr(runner, "eth0", "172.20.0.5/16")
	p, err := New(Options{Subnet: "192.168.200.0/24", Capacity: 4, HostIface: "eth0", Runner: runner})
	require.NoError(t, err)
	p.enableIPForward = func() error { return nil }

	require.NoError(t, p.WarmUp(context.Background()))

	var batchCalls int
	for _, c := range runner.Calls() {
		if contains(c, "-batch") {
			batchCalls++
		}
	}
	assert.Equal(t, 8, batchCalls,
		"each of the 4 slots' host-side and netns-side setup must each collapse into one ip -batch invocation")
}

func TestVethProvisioner_ProvisionMakesNoRunnerCallsAfterWarmUp(t *testing.T) {
	runner := newFakeRunner()
	p := newTestProvisioner(t, runner) // WarmUp already ran here
	before := len(runner.Calls())

	_, err := p.Provision(context.Background(), "a")
	require.NoError(t, err)

	assert.Equal(t, before, len(runner.Calls()),
		"Provision must be pure bookkeeping once the pool is warmed up - no ip call on the request path")
}

func TestVethProvisioner_ProvisionBeforeWarmUpReturnsAnError(t *testing.T) {
	runner := newFakeRunner()
	stubHostIfaceAddr(runner, "eth0", "172.20.0.5/16")
	p, err := New(Options{Subnet: "192.168.200.0/24", Capacity: 4, HostIface: "eth0", Runner: runner})
	require.NoError(t, err)

	_, err = p.Provision(context.Background(), "a")
	require.Error(t, err)
}

func TestVethProvisioner_ProvisionReturnsTheNetnsPath(t *testing.T) {
	runner := newFakeRunner()
	p := newTestProvisioner(t, runner)

	alloc, err := p.Provision(context.Background(), "exec_container-abc")
	require.NoError(t, err)
	assert.Equal(t, "/var/run/netns/readie-pool-0", alloc.NetnsPath, "the netns is a pool slot's, independent of the sandbox id")
	assert.NotEmpty(t, alloc.SandboxIP)
	assert.NotEmpty(t, alloc.HostIP)
}

func TestVethProvisioner_WarmUpFailsIfAnySlotsSetupFails(t *testing.T) {
	runner := newFakeRunner()
	stubHostIfaceAddr(runner, "eth0", "172.20.0.5/16")
	// The host-side batch is the first of the two per-slot ip -batch calls;
	// failing it exercises the same failure path a failure in any of its five
	// underlying commands would (they're no longer individually
	// distinguishable once batched - see the plan this shipped with).
	runner.failOn["ip -batch"] = assertAnError()
	p, err := New(Options{Subnet: "192.168.200.0/24", Capacity: 1, HostIface: "eth0", Runner: runner})
	require.NoError(t, err)
	p.enableIPForward = func() error { return nil }

	require.Error(t, p.WarmUp(context.Background()))

	// A provisioner that failed to warm up must not silently serve requests
	// against a pool that was never actually built.
	_, err = p.Provision(context.Background(), "a")
	require.Error(t, err)
}

func TestVethProvisioner_ReleaseDoesNotDeleteTheNamespace(t *testing.T) {
	runner := newFakeRunner()
	p := newTestProvisioner(t, runner)

	_, err := p.Provision(context.Background(), "a")
	require.NoError(t, err)
	before := len(runner.Calls())

	require.NoError(t, p.Release(context.Background(), "a"))

	assert.Equal(t, before, len(runner.Calls()),
		"Release recycles the slot for reuse - it must not tear the namespace down")
}

func TestVethProvisioner_ReleasedSlotIsReusedByTheNextID(t *testing.T) {
	runner := newFakeRunner()
	stubHostIfaceAddr(runner, "eth0", "172.20.0.5/16")
	p, err := New(Options{Subnet: "192.168.200.0/24", Capacity: 1, HostIface: "eth0", Runner: runner})
	require.NoError(t, err)
	p.enableIPForward = func() error { return nil }
	require.NoError(t, p.WarmUp(context.Background()))

	first, err := p.Provision(context.Background(), "a")
	require.NoError(t, err)
	require.NoError(t, p.Release(context.Background(), "a"))
	before := len(runner.Calls())

	second, err := p.Provision(context.Background(), "b")
	require.NoError(t, err)

	assert.Equal(t, first, second, "the recycled slot's netns/veth/addresses are handed to the next id unchanged")
	assert.Equal(t, before, len(runner.Calls()), "reusing a released slot must not redo any ip work")
}

func TestVethProvisioner_ReleaseOfAnUnprovisionedIDIsNotAnError(t *testing.T) {
	runner := newFakeRunner()
	p := newTestProvisioner(t, runner)

	// This id was never Provisioned by this process - e.g. an orphan a
	// crashed predecessor left behind - yet Release must still succeed.
	require.NoError(t, p.Release(context.Background(), "orphan"))
}

func TestVethProvisioner_CloseDeletesEverySlotsNamespace(t *testing.T) {
	runner := newFakeRunner()
	p := newTestProvisioner(t, runner) // Capacity: 4

	require.NoError(t, p.Close(context.Background()))

	var deletes []string
	for _, c := range runner.Calls() {
		if len(c) == 4 && c[0] == "ip" && c[1] == "netns" && c[2] == "delete" {
			deletes = append(deletes, c[3])
		}
	}
	assert.ElementsMatch(t, []string{"readie-pool-0", "readie-pool-1", "readie-pool-2", "readie-pool-3"}, deletes)
}

func TestVethProvisioner_CloseToleratesAlreadyGone(t *testing.T) {
	runner := newFakeRunner()
	runner.failOn["ip netns delete"] = commandErrorWithStderr("Cannot remove namespace file: No such file or directory")
	p := newTestProvisioner(t, runner)

	require.NoError(t, p.Close(context.Background()))
}

func assertAnError() error {
	return &CommandError{Binary: "ip", Args: []string{"addr", "add"}, ExitCode: 1, Stderr: "boom"}
}

func commandErrorWithStderr(stderr string) error {
	return &CommandError{Binary: "ip", Args: []string{"netns", "delete"}, ExitCode: 1, Stderr: stderr}
}
