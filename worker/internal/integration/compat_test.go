package integration

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
)

// checkpointLister is the read side of the artifact registry a test needs to
// see whether the worker kept or refused its baked checkpoints.
type checkpointLister interface {
	Checkpoints() []string
}

// The baked manifest records a placeholder spec fingerprint that no real worker
// can reproduce. With strict compatibility on, the worker must refuse the
// checkpoints at startup and serve cold starts only — rather than advertise
// checkpoints every restore would silently fail to use.
func TestStrictCompat_RefusesIncompatibleCheckpoints(t *testing.T) {
	h := newHarness(t, withConfig(func(c *config.Config) {
		c.CheckpointStrictCompat = true
	}))

	// waitUntilServing has already returned, so the startup-time check has run.
	lister, ok := h.Artifacts.(checkpointLister)
	require.True(t, ok)
	assert.Empty(t, lister.Checkpoints(), "strict worker must drop the mismatched checkpoints")

	// And it still serves: a cold execution succeeds over the refused checkpoints.
	responses, err := execute(t, h, "", []byte("pickled-"), []byte("payload"))
	require.NoError(t, err)
	require.NotEmpty(t, responses)
	assert.True(t, responses[0].GetSuccess())
}

// The same mismatch, left tolerant, keeps the checkpoints: the operator has
// opted to accept that restores may fall back to cold starts.
func TestTolerantCompat_KeepsMismatchedCheckpoints(t *testing.T) {
	h := newHarness(t, withConfig(func(c *config.Config) {
		c.CheckpointStrictCompat = false
	}))

	lister, ok := h.Artifacts.(checkpointLister)
	require.True(t, ok)
	assert.NotEmpty(t, lister.Checkpoints())
}
