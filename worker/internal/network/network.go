// Package network provisions per-sandbox Linux network namespaces so gVisor's
// --network=sandbox mode has something real to join.
//
// gVisor itself creates no networking under that mode: at sandbox start, the
// sentry joins whatever network namespace the OCI spec names and harvests
// whatever non-loopback interfaces already exist there. Normally a CNI plugin
// builds that namespace before a container runtime invokes runsc; this worker
// has no CNI, so this package is the host-side half CNI would otherwise
// automate - a namespace, a veth pair straddling it, IP addresses on both
// ends, and host-side NAT so the sandbox side can actually reach the internet.
package network

import "context"

// Allocation is the addressing Provision assigned one sandbox.
type Allocation struct {
	// NetnsPath is what the sandbox's OCI spec must set NetworkNamespace.Path
	// to. It is excluded from the checkpoint fingerprint (only a namespace's
	// presence, never its path, is hashed - see runsc.Fingerprint), so it
	// never needs to match anything a checkpoint was captured under.
	NetnsPath string
	// SandboxIP is the address (with prefix, e.g. "192.168.200.5/30") the
	// sandbox's own end of the veth pair carries.
	SandboxIP string
	// HostIP is the address (with prefix) the host's end of the veth pair
	// carries. Also the sandbox's default gateway.
	HostIP string
}

// Provisioner sets up and tears down a sandbox's private network namespace.
//
// A container.Manager depends on this and only this; ip(8)/iptables(8) stay
// behind it exactly the way container.Manager never learns the sandbox
// runtime's name (sandbox.Port) or the filesystem's (container.FS).
type Provisioner interface {
	// WarmUp pre-provisions every capacity slot's netns, veth pair, addresses
	// and shared NAT/forwarding rules, so the cost of building them happens
	// once at worker startup instead of on every Provision call. Must be
	// called exactly once, before the first Provision call.
	WarmUp(ctx context.Context) error

	// Provision assigns id a pre-warmed netns and veth pair, addressed and
	// ready to use. Idempotent per id: calling it again for an id that
	// already holds a slot returns the same Allocation. Returns an error if
	// called before WarmUp has completed.
	Provision(ctx context.Context, id string) (Allocation, error)

	// Release returns id's slot to the pool for reuse by a future id. The
	// underlying netns and veth pair are not torn down - they outlive any one
	// container, ready to be handed to the next one.
	//
	// Idempotent: releasing an id that was never provisioned, or already
	// released - including one this process never provisioned itself, such as
	// an orphan a crashed predecessor left behind - is not an error, mirroring
	// how sandbox.Port's own Stop/Remove tolerate an already-gone container.
	Release(ctx context.Context, id string) error

	// Close tears down every slot's netns (and, as a consequence, its veth
	// pair) and is called once during worker shutdown, after every container
	// has been destroyed and released. It does not touch the shared
	// NAT/forwarding rules.
	Close(ctx context.Context) error
}
