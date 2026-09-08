// Package fakenetwork provides an in-memory network.Provisioner for tests.
//
// Substituting here is what keeps worker/internal/integration runnable without
// real root/netns/iptables access, the same way fakesandbox keeps it runnable
// without gVisor.
package fakenetwork

import (
	"context"
	"fmt"
	"sync"

	"github.com/illinoisdata/readie/worker/internal/network"
)

// Provisioner is an in-memory network.Provisioner, safe for concurrent use.
//
// It fabricates deterministic fake paths and addresses under TEST-NET-1
// (RFC 5737, 192.0.2.0/24) so a fake allocation can never collide with
// anything a real Provisioner would ever hand out.
type Provisioner struct {
	mu         sync.Mutex
	allocated  map[string]network.Allocation
	nextSlot   int
	failures   map[string]error
	provisions []string // ids, in call order, including repeats
	releases   []string
}

// New returns an empty fake.
func New() *Provisioner {
	return &Provisioner{
		allocated: make(map[string]network.Allocation),
		failures:  make(map[string]error),
	}
}

var _ network.Provisioner = (*Provisioner)(nil)

// WarmUp implements network.Provisioner. The fake has nothing to warm up.
func (p *Provisioner) WarmUp(_ context.Context) error { return nil }

// Close implements network.Provisioner. The fake has nothing to tear down.
func (p *Provisioner) Close(_ context.Context) error { return nil }

// FailOn makes the named method ("Provision" or "Release") return err on its
// next call. The failure is one-shot: it clears itself once triggered, the
// same way a transient real failure would not repeat on its own.
func (p *Provisioner) FailOn(method string, err error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.failures[method] = err
}

// Provision implements network.Provisioner.
func (p *Provisioner) Provision(_ context.Context, id string) (network.Allocation, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	p.provisions = append(p.provisions, id)
	if err := p.failures["Provision"]; err != nil {
		delete(p.failures, "Provision")
		return network.Allocation{}, err
	}

	if alloc, ok := p.allocated[id]; ok {
		return alloc, nil
	}

	slot := p.nextSlot
	p.nextSlot++
	alloc := network.Allocation{
		NetnsPath: fmt.Sprintf("/fake/netns/%s", id),
		SandboxIP: fmt.Sprintf("192.0.2.%d/30", slot*4+2),
		HostIP:    fmt.Sprintf("192.0.2.%d/30", slot*4+1),
	}
	p.allocated[id] = alloc
	return alloc, nil
}

// Release implements network.Provisioner.
func (p *Provisioner) Release(_ context.Context, id string) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	p.releases = append(p.releases, id)
	if err := p.failures["Release"]; err != nil {
		delete(p.failures, "Release")
		return err
	}
	delete(p.allocated, id)
	return nil
}

// Allocations returns every currently-live allocation, keyed by id.
func (p *Provisioner) Allocations() map[string]network.Allocation {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make(map[string]network.Allocation, len(p.allocated))
	for k, v := range p.allocated {
		out[k] = v
	}
	return out
}

// Provisions returns every id Provision was called with, in call order,
// including repeats.
func (p *Provisioner) Provisions() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.provisions...)
}

// Releases returns every id Release was called with, in call order.
func (p *Provisioner) Releases() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.releases...)
}
