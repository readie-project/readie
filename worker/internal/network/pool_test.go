package network

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestIPPool_AcquireGivesDistinctSlots(t *testing.T) {
	pool, err := newIPPool("192.168.200.0/24", 4)
	require.NoError(t, err)

	sandboxA, hostA, err := pool.acquire("a")
	require.NoError(t, err)
	sandboxB, hostB, err := pool.acquire("b")
	require.NoError(t, err)

	assert.NotEqual(t, sandboxA, sandboxB)
	assert.NotEqual(t, hostA, hostB)
	assert.NotEqual(t, sandboxA, hostA, "a sandbox and its own host peer must differ")
}

func TestIPPool_AcquireIsIdempotentPerID(t *testing.T) {
	pool, err := newIPPool("192.168.200.0/24", 4)
	require.NoError(t, err)

	sandboxA, hostA, err := pool.acquire("a")
	require.NoError(t, err)
	sandboxAgain, hostAgain, err := pool.acquire("a")
	require.NoError(t, err)

	assert.Equal(t, sandboxA, sandboxAgain)
	assert.Equal(t, hostA, hostAgain)
}

func TestIPPool_ReleaseFreesTheSlotForReuse(t *testing.T) {
	pool, err := newIPPool("192.168.200.0/24", 1)
	require.NoError(t, err)

	sandboxA, _, err := pool.acquire("a")
	require.NoError(t, err)
	pool.release("a")

	sandboxB, _, err := pool.acquire("b")
	require.NoError(t, err)
	assert.Equal(t, sandboxA, sandboxB, "the freed slot must be reused, not left stranded")
}

func TestIPPool_ExhaustionIsAnError(t *testing.T) {
	pool, err := newIPPool("192.168.200.0/24", 1)
	require.NoError(t, err)

	_, _, err = pool.acquire("a")
	require.NoError(t, err)

	_, _, err = pool.acquire("b")
	require.Error(t, err)
}

func TestIPPool_ReleaseOfAnUnknownIDIsANoOp(t *testing.T) {
	pool, err := newIPPool("192.168.200.0/24", 1)
	require.NoError(t, err)

	// An id this pool never allocated - e.g. an orphan a crashed predecessor's
	// own, separate pool once tracked - must not panic or corrupt state.
	pool.release("never-allocated")

	_, _, err = pool.acquire("a")
	require.NoError(t, err)
}

func TestNewIPPool_RejectsCapacityLargerThanTheSubnet(t *testing.T) {
	// A /30 per slot: a /29 holds exactly 2.
	_, err := newIPPool("192.168.200.0/29", 3)
	require.Error(t, err)
}

func TestNewIPPool_RejectsAnUnparsableSubnet(t *testing.T) {
	_, err := newIPPool("not-a-subnet", 1)
	require.Error(t, err)
}
