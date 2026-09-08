package network

import (
	"fmt"
	"net/netip"
	"sync"
)

// ipPool hands out one /30 per container from a fixed subnet - a
// point-to-point pair (one host address, one sandbox address) rather than a
// shared /24 with per-host addresses, so no sandbox's veth is ever on the same
// link as another's. Mirrors container.Manager's own mutex-protected live map:
// a small, fixed-capacity table is enough, since capacity is sized off how
// many containers this worker ever runs at once.
type ipPool struct {
	mu    sync.Mutex
	base  uint32 // subnet base address, host byte order
	slots int    // usable /30 slots in the subnet
	next  int    // next never-used slot
	free  []int  // released slots, preferred over next so low slot numbers churn
	inUse map[string]int
}

// newIPPool builds a pool of capacity /30 slots carved from subnet.
func newIPPool(subnet string, capacity int) (*ipPool, error) {
	prefix, err := netip.ParsePrefix(subnet)
	if err != nil {
		return nil, fmt.Errorf("parse subnet %q: %w", subnet, err)
	}
	if !prefix.Addr().Is4() {
		return nil, fmt.Errorf("subnet %q must be IPv4", subnet)
	}
	if capacity <= 0 {
		return nil, fmt.Errorf("capacity must be positive, got %d", capacity)
	}

	available := 1 << (32 - prefix.Bits() - 2) // number of /30s the subnet holds
	if capacity > available {
		return nil, fmt.Errorf("subnet %s holds only %d /30 slots, capacity %d requested", subnet, available, capacity)
	}

	addr4 := prefix.Masked().Addr().As4()
	base := uint32(addr4[0])<<24 | uint32(addr4[1])<<16 | uint32(addr4[2])<<8 | uint32(addr4[3])

	return &ipPool{base: base, slots: capacity, inUse: make(map[string]int)}, nil
}

// acquire returns id's sandbox and host addresses, allocating a fresh slot if
// id holds none yet. Calling it again for an id that already holds a slot
// returns that same slot's addresses, which is what makes Provision
// idempotent.
func (p *ipPool) acquire(id string) (sandboxIP, hostIP netip.Addr, err error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	slot, ok := p.inUse[id]
	if !ok {
		switch {
		case len(p.free) > 0:
			slot, p.free = p.free[len(p.free)-1], p.free[:len(p.free)-1]
		case p.next < p.slots:
			slot, p.next = p.next, p.next+1
		default:
			return netip.Addr{}, netip.Addr{}, fmt.Errorf("no free network slot (capacity %d)", p.slots)
		}
		p.inUse[id] = slot
	}
	return p.addrs(slot)
}

// slot returns id's currently-held slot number, for constructing veth
// interface names. Only meaningful immediately after a successful acquire.
func (p *ipPool) slot(id string) (int, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	slot, ok := p.inUse[id]
	return slot, ok
}

// release returns id's slot to the free list. A no-op for an id this pool
// never allocated - e.g. an orphan a crashed predecessor's own (separate,
// now-empty) pool once tracked - which is what makes it safe to call
// unconditionally from Release.
func (p *ipPool) release(id string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if slot, ok := p.inUse[id]; ok {
		delete(p.inUse, id)
		p.free = append(p.free, slot)
	}
}

func (p *ipPool) addrs(slot int) (sandboxIP, hostIP netip.Addr, err error) {
	slotBase := p.base + uint32(slot)*4 //nolint:gosec // slot is bounds-checked against p.slots at acquire time
	return addrFromUint32(slotBase + 2), addrFromUint32(slotBase + 1), nil
}

func addrFromUint32(v uint32) netip.Addr {
	return netip.AddrFrom4([4]byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)})
}
