package network

import (
	"context"
	"fmt"
	"log/slog"
	"net/netip"
	"os"
	"strings"
	"sync"
)

// defaultSubnet is where every /30 slot comes from. 64 slots (/24 minus
// network/broadcast reservations per /30) is comfortably above any single
// worker's realistic WORKER_MAX_EXECUTORS; a worker wanting more is out of
// scope for now (see the package README/plan this shipped with).
const defaultSubnet = "192.168.200.0/24"

// linkLocalSubnet covers 169.254.169.254, the address every major cloud
// provider (AWS, GCP, Azure, ...) serves its instance metadata service on - a
// classic SSRF target, and one that has no legitimate reason to be reachable
// through a sandbox's forwarded egress.
const linkLocalSubnet = "169.254.0.0/16"

// Options configures a VethProvisioner.
type Options struct {
	// Subnet is the private range every /30 slot is carved from. Defaults to
	// defaultSubnet.
	Subnet string
	// Capacity is the number of concurrent sandboxes to reserve slots for.
	// Required.
	Capacity int
	// HostIface is the interface NAT exits through. Empty auto-detects it from
	// the default route at the first Provision call.
	HostIface string

	IPBinary       string // default "ip"
	IPTablesBinary string // default "iptables"
	Runner         CommandRunner
	Log            *slog.Logger
}

func (o Options) withDefaults() Options {
	if o.Subnet == "" {
		o.Subnet = defaultSubnet
	}
	if o.IPBinary == "" {
		o.IPBinary = "ip"
	}
	if o.IPTablesBinary == "" {
		o.IPTablesBinary = "iptables"
	}
	if o.Runner == nil {
		o.Runner = ExecRunner{}
	}
	if o.Log == nil {
		o.Log = slog.Default()
	}
	return o
}

// VethProvisioner is the real Provisioner: a netns + veth pair per sandbox,
// with one-time host NAT/forwarding shared across all of them.
type VethProvisioner struct {
	opts Options
	pool *ipPool
	log  *slog.Logger

	natOnce sync.Once
	natErr  error
	// enableIPForward is overridable in tests; defaults to writing procfs directly.
	enableIPForward func() error

	mu       sync.Mutex
	warmedUp bool
}

var _ Provisioner = (*VethProvisioner)(nil)

// New builds a VethProvisioner. Capacity must be positive.
func New(opts Options) (*VethProvisioner, error) {
	opts = opts.withDefaults()
	pool, err := newIPPool(opts.Subnet, opts.Capacity)
	if err != nil {
		return nil, fmt.Errorf("build ip pool: %w", err)
	}
	return &VethProvisioner{
		opts: opts, pool: pool, log: opts.Log,
		enableIPForward: enableIPForwardViaProcfs,
	}, nil
}

// WarmUp implements Provisioner. It builds every slot's netns and veth pair
// once, up front, so Provision never has to create either on the request
// path - only re-address them (see Provision).
func (p *VethProvisioner) WarmUp(ctx context.Context) error {
	p.natOnce.Do(func() { p.natErr = p.setupNAT(ctx) })
	if p.natErr != nil {
		return fmt.Errorf("set up host NAT: %w", p.natErr)
	}

	for slot := 0; slot < p.opts.Capacity; slot++ {
		name := poolNetnsName(slot)
		hostVeth, sandboxVeth := vethNames(slot)
		sandboxIP, hostIP, err := p.pool.addrs(slot)
		if err != nil {
			return fmt.Errorf("compute addresses for slot %d: %w", slot, err)
		}

		if err := p.runBatch(ctx, hostSetupCommands(name, hostVeth, sandboxVeth, hostIP)); err != nil {
			return fmt.Errorf("warm up sandbox network slot %d (host-side): %w", slot, err)
		}
		if err := p.runNetnsBatch(ctx, name, netnsSetupCommands(sandboxVeth, sandboxIP, hostIP)); err != nil {
			return fmt.Errorf("warm up sandbox network slot %d (netns-side): %w", slot, err)
		}
	}

	p.mu.Lock()
	p.warmedUp = true
	p.mu.Unlock()
	return nil
}

// Provision implements Provisioner.
//
// Every slot's netns and veth pair already exist - WarmUp built them - so
// this never creates either on the request path. It does re-address the
// slot's sandbox-side interface and re-point its default route, though: a
// gVisor sandbox given a real interface under --network=sandbox takes
// exclusive ownership of it for that sandbox's lifetime rather than handing
// it back configured, so a slot a previous sandbox actually used comes back
// from Release with its address and route gone, not merely idle. Re-running
// WarmUp's netns-side batch here (idempotently - see netnsSetupCommands)
// heals that unconditionally, whether or not this is the slot's first use, so
// every Allocation this returns is one the sandbox that gets it can actually
// route out of. The cost is one `ip netns exec` batch per request instead of
// zero, but a slot silently unable to reach the network is worse.
func (p *VethProvisioner) Provision(ctx context.Context, id string) (Allocation, error) {
	p.mu.Lock()
	warmedUp := p.warmedUp
	p.mu.Unlock()
	if !warmedUp {
		return Allocation{}, fmt.Errorf("sandbox network pool not warmed up yet")
	}

	sandboxIP, hostIP, err := p.pool.acquire(id)
	if err != nil {
		return Allocation{}, fmt.Errorf("allocate network slot: %w", err)
	}
	slot, _ := p.pool.slot(id)
	name := poolNetnsName(slot)
	_, sandboxVeth := vethNames(slot)

	if err := p.runNetnsBatch(ctx, name, netnsSetupCommands(sandboxVeth, sandboxIP, hostIP)); err != nil {
		p.pool.release(id)
		return Allocation{}, fmt.Errorf("re-address sandbox network slot %d: %w", slot, err)
	}

	return Allocation{
		NetnsPath: netnsPath(name),
		SandboxIP: sandboxIP.String() + "/30",
		HostIP:    hostIP.String() + "/30",
	}, nil
}

// Release implements Provisioner.
//
// The slot's netns and veth pair stay up - only the id-to-slot bookkeeping is
// undone here. Their address and route do not: see Provision, which restores
// them unconditionally on the slot's next handout rather than assuming
// Release's occupant left them intact.
func (p *VethProvisioner) Release(_ context.Context, id string) error {
	p.pool.release(id)
	return nil
}

// Close implements Provisioner.
func (p *VethProvisioner) Close(ctx context.Context) error {
	for slot := 0; slot < p.opts.Capacity; slot++ {
		name := poolNetnsName(slot)
		if _, err := p.opts.Runner.Output(ctx, p.opts.IPBinary, "netns", "delete", name); err != nil {
			if looksAlreadyGone(err) {
				continue
			}
			return fmt.Errorf("delete sandbox network namespace %s: %w", name, err)
		}
	}
	return nil
}

// setupNAT enables forwarding and installs the one shared MASQUERADE/FORWARD
// rule set every provisioned sandbox rides on. Idempotent across a restart:
// each rule is added only if a check for it fails, so a worker that crashes
// and comes back up does not accumulate duplicates.
func (p *VethProvisioner) setupNAT(ctx context.Context) error {
	if err := p.enableIPForward(); err != nil {
		return fmt.Errorf("enable ip forwarding: %w", err)
	}

	hostIface := p.opts.HostIface
	if hostIface == "" {
		var err error
		hostIface, err = defaultRouteIface(ctx, p.opts.Runner, p.opts.IPBinary)
		if err != nil {
			return fmt.Errorf("determine host interface for NAT: %w", err)
		}
	}

	// hostIface is also how this worker reaches every sibling container on the
	// same Docker network (router in particular - unauthenticated by default,
	// see SECURITY.md), so unrestricted egress through it would double as a
	// path to them. Determining this fails closed: if it cannot be determined,
	// NAT setup - and so every Provision - fails, rather than silently
	// standing up unrestricted egress.
	hostSubnet, err := ifaceSubnet(ctx, p.opts.Runner, p.opts.IPBinary, hostIface)
	if err != nil {
		return fmt.Errorf("determine %s's own subnet, to exclude it from sandbox egress: %w", hostIface, err)
	}

	rules := [][]string{
		{"-t", "nat", "-A", "POSTROUTING", "-s", p.opts.Subnet, "-o", hostIface, "-j", "MASQUERADE"},
		{"-A", "FORWARD", "-i", hostIface, "-o", "veth+", "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"},
		// Both DROPs before the general ACCEPT below: iptables evaluates a
		// chain in order and stops at the first match, so these have to come
		// first to actually take effect.
		{"-A", "FORWARD", "-s", p.opts.Subnet, "-i", "veth+", "-d", hostSubnet, "-j", "DROP"},
		{"-A", "FORWARD", "-s", p.opts.Subnet, "-i", "veth+", "-d", linkLocalSubnet, "-j", "DROP"},
		{"-A", "FORWARD", "-s", p.opts.Subnet, "-i", "veth+", "-o", hostIface, "-j", "ACCEPT"},
	}
	for _, rule := range rules {
		checkArgs := append([]string{}, rule...)
		checkArgs[indexOf(checkArgs, "-A")] = "-C"
		if _, err := p.opts.Runner.Output(ctx, p.opts.IPTablesBinary, checkArgs...); err == nil {
			continue // already present
		}
		if _, err := p.opts.Runner.Output(ctx, p.opts.IPTablesBinary, rule...); err != nil {
			return fmt.Errorf("install iptables rule %v: %w", rule, err)
		}
	}
	return nil
}

func vethNames(slot int) (host, sandbox string) {
	return fmt.Sprintf("veth-h%d", slot), fmt.Sprintf("veth-s%d", slot)
}

// poolNetnsName is the netns name a pool slot uses, independent of whatever
// sandbox id Provision later assigns that slot to - WarmUp creates it before
// any id exists.
func poolNetnsName(slot int) string {
	return fmt.Sprintf("readie-pool-%d", slot)
}

// hostSetupCommands returns the ip(8) batch lines that build the host side of
// id's veth pair: create the namespace, create the pair, move the sandbox end
// into it, and address+bring-up the host end. Run in the current (host)
// namespace, before the sandbox end is reachable at all.
func hostSetupCommands(id, hostVeth, sandboxVeth string, hostIP netip.Addr) []string {
	return []string{
		"netns add " + id,
		"link add " + hostVeth + " type veth peer name " + sandboxVeth,
		"link set " + sandboxVeth + " netns " + id,
		"addr add " + hostIP.String() + "/30 dev " + hostVeth,
		"link set " + hostVeth + " up",
	}
}

// netnsSetupCommands returns the ip(8) batch lines that finish the sandbox
// side of the pair once inside id's namespace: address+bring-up the sandbox
// end, bring up loopback, and point the default route at the host end.
//
// `replace`, not `add`, for the address and route: this batch runs both at
// WarmUp (nothing to replace yet) and again on every Provision (the previous
// occupant's gVisor sandbox already claimed this slot's address+route, see
// Provision), so it has to be safe to re-apply over either a bare interface or
// one already carrying them - `add` errors on the latter.
func netnsSetupCommands(sandboxVeth string, sandboxIP, hostIP netip.Addr) []string {
	return []string{
		"addr replace " + sandboxIP.String() + "/30 dev " + sandboxVeth,
		"link set " + sandboxVeth + " up",
		"link set lo up",
		"route replace default via " + hostIP.String(),
	}
}

// runBatch runs lines as one `ip -batch` invocation in the current namespace.
func (p *VethProvisioner) runBatch(ctx context.Context, lines []string) error {
	path, err := writeBatchFile(lines)
	if err != nil {
		return err
	}
	defer func() { _ = os.Remove(path) }()

	if _, err := p.opts.Runner.Output(ctx, p.opts.IPBinary, "-batch", path); err != nil {
		return err
	}
	return nil
}

// runNetnsBatch runs lines as one `ip -batch` invocation after entering
// namespace netns, via `ip netns exec <netns> ip -batch <file>`. The batch
// file lives in the host's filesystem view, which `ip netns exec` shares
// (it unshares only the network - and, for its own /etc/netns handling,
// mount - namespace, not the underlying filesystem), so the same path is
// still readable once inside.
func (p *VethProvisioner) runNetnsBatch(ctx context.Context, netns string, lines []string) error {
	path, err := writeBatchFile(lines)
	if err != nil {
		return err
	}
	defer func() { _ = os.Remove(path) }()

	if _, err := p.opts.Runner.Output(ctx, p.opts.IPBinary,
		"netns", "exec", netns, p.opts.IPBinary, "-batch", path); err != nil {
		return err
	}
	return nil
}

// writeBatchFile writes lines, one per line, to a fresh temp file for
// `ip -batch` to read.
func writeBatchFile(lines []string) (string, error) {
	f, err := os.CreateTemp("", "readie-netns-batch-*")
	if err != nil {
		return "", fmt.Errorf("create ip batch file: %w", err)
	}
	defer func() { _ = f.Close() }()

	if _, err := f.WriteString(strings.Join(lines, "\n") + "\n"); err != nil {
		_ = os.Remove(f.Name())
		return "", fmt.Errorf("write ip batch file: %w", err)
	}
	return f.Name(), nil
}

func netnsPath(name string) string {
	return "/var/run/netns/" + name
}

// procIPForwardPath is where the kernel exposes the forwarding toggle. Writing
// here directly avoids depending on the sysctl(8) binary (part of procps, not
// installed alongside this package's other two dependencies, iproute2 and
// iptables).
const procIPForwardPath = "/proc/sys/net/ipv4/ip_forward"

func enableIPForwardViaProcfs() error {
	if err := os.WriteFile(procIPForwardPath, []byte("1"), 0o644); err != nil { //nolint:gosec // a kernel toggle, not sensitive file content
		return fmt.Errorf("write %s: %w", procIPForwardPath, err)
	}
	return nil
}

func indexOf(ss []string, s string) int {
	for i, v := range ss {
		if v == s {
			return i
		}
	}
	return -1
}

// defaultRouteIface asks the kernel which interface the default route exits
// through, e.g. "eth0" - Docker's usual bridge-network interface name inside a
// container - by parsing `ip route show default`'s "... dev <iface> ...".
func defaultRouteIface(ctx context.Context, runner CommandRunner, ipBinary string) (string, error) {
	result, err := runner.Output(ctx, ipBinary, "-4", "route", "show", "default")
	if err != nil {
		return "", fmt.Errorf("read default route: %w", err)
	}
	fields := strings.Fields(result.Stdout)
	for i, f := range fields {
		if f == "dev" && i+1 < len(fields) {
			return fields[i+1], nil
		}
	}
	return "", fmt.Errorf("no default route found in %q", result.Stdout)
}

// ifaceSubnet returns the network address (not the host address) of iface's
// own IPv4 address, e.g. "172.20.0.0/16" for an interface holding
// "172.20.0.5/16". This is what the egress path must NOT forward a sandbox's
// traffic to: hostIface is also how this worker reaches every sibling
// container on the same Docker network (the router, in particular, which by
// default has no authentication of its own - see SECURITY.md), so without
// this a sandbox's real network access would double as a path to them.
func ifaceSubnet(ctx context.Context, runner CommandRunner, ipBinary, iface string) (string, error) {
	result, err := runner.Output(ctx, ipBinary, "-4", "-o", "addr", "show", "dev", iface)
	if err != nil {
		return "", fmt.Errorf("read address of %s: %w", iface, err)
	}
	fields := strings.Fields(result.Stdout)
	for i, f := range fields {
		if f == "inet" && i+1 < len(fields) {
			prefix, err := netip.ParsePrefix(fields[i+1])
			if err != nil {
				return "", fmt.Errorf("parse address %q of %s: %w", fields[i+1], iface, err)
			}
			return prefix.Masked().String(), nil
		}
	}
	return "", fmt.Errorf("no IPv4 address found on %s in %q", iface, result.Stdout)
}
