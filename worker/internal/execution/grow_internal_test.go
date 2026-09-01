package execution

import (
	"context"
	"fmt"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/logging"
)

// growRecorder is a ContainerService that only implements Grow; the growth
// watcher calls nothing else.
type growRecorder struct {
	ContainerService
	grown []int64
}

func (g *growRecorder) Grow(_ context.Context, _ string, memBytes int64) error {
	g.grown = append(g.grown, memBytes)
	return nil
}

func growRunner(t *testing.T, workerMemTotal int64) (*Runner, *growRecorder) {
	t.Helper()
	rec := &growRecorder{}
	r := &Runner{
		containers: rec,
		cfg: RunnerConfig{
			WorkerMemTotal: workerMemTotal,
		}.withDefaults(), // threshold 0.9, factor 2.0, headroom 0.9
		log: logging.Discard(),
	}
	return r, rec
}

func TestMaybeGrow_GrowsWhenUsageCrossesTheThreshold(t *testing.T) {
	r, rec := growRunner(t, 4<<30)

	// 950/1000 is past the 0.9 threshold; factor 2 targets 2000, well under the
	// worker-capacity ceiling.
	r.maybeGrow(context.Background(), "c", 950, 1000, 0, r.log)

	require.Equal(t, []int64{2000}, rec.grown)
}

func TestMaybeGrow_IsCappedByTheRequestMax(t *testing.T) {
	r, rec := growRunner(t, 4<<30)

	// The request's own ceiling (1500) wins over factor*limit (2000).
	r.maybeGrow(context.Background(), "c", 950, 1000, 1500, r.log)

	require.Equal(t, []int64{1500}, rec.grown)
}

func TestMaybeGrow_DoesNothingBelowTheThreshold(t *testing.T) {
	r, rec := growRunner(t, 4<<30)

	r.maybeGrow(context.Background(), "c", 500, 1000, 0, r.log)

	assert.Empty(t, rec.grown)
}

func TestMaybeGrow_DoesNothingWithoutAKnownCeiling(t *testing.T) {
	r, rec := growRunner(t, 0) // no worker capacity reported

	r.maybeGrow(context.Background(), "c", 950, 1000, 0, r.log)

	assert.Empty(t, rec.grown, "no per-request max and no capacity means no ceiling")
}

func TestGrownRequest_ExpandsTowardTheCeiling(t *testing.T) {
	r, _ := growRunner(t, 0)
	req := Request{Alloc: container.Allocation{Budgets: []container.Budget{
		{Kind: container.KindMemory, Alloc: 1 << 20, Max: 4 << 20},
	}}}

	grown, ok := r.grownRequest(req)

	require.True(t, ok)
	assert.Equal(t, int64(2<<20), grown.Alloc.Memory().Alloc, "factor 2 of the initial")
	assert.Equal(t, int64(4<<20), grown.Alloc.Memory().Max, "ceiling is preserved")
}

func TestGrownRequest_ReportsNoRoomAtTheCeiling(t *testing.T) {
	r, _ := growRunner(t, 0)
	req := Request{Alloc: container.Allocation{Budgets: []container.Budget{
		{Kind: container.KindMemory, Alloc: 4 << 20, Max: 4 << 20},
	}}}

	_, ok := r.grownRequest(req)

	assert.False(t, ok, "already at max, so a retry cannot grow it")
}

func TestLooksLikeOOM(t *testing.T) {
	assert.True(t, looksLikeOOM(fmt.Errorf("relay: %w", executor.ErrNoResponse)))
	assert.True(t, looksLikeOOM(fmt.Errorf("relay: %w", executor.ErrTruncatedResponse)))
	assert.False(t, looksLikeOOM(fmt.Errorf("some other failure")))
}
