package app

import (
	"testing"

	"github.com/stretchr/testify/assert"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/artifact"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
)

const (
	workerFingerprint = "sha256:worker"
	workerVersion     = "runsc version worker"
)

func matchingManifest() artifact.Manifest {
	return artifact.Manifest{SpecFingerprint: workerFingerprint, RunscVersion: workerVersion}
}

func TestCheckpointCompatReasons_Compatible(t *testing.T) {
	reasons := checkpointCompatReasons(matchingManifest(), workerFingerprint, workerVersion)
	assert.Empty(t, reasons)
}

func TestCheckpointCompatReasons_FingerprintMismatch(t *testing.T) {
	m := matchingManifest()
	m.SpecFingerprint = "sha256:captured-elsewhere"

	reasons := checkpointCompatReasons(m, workerFingerprint, workerVersion)

	assert.Len(t, reasons, 1)
	assert.Contains(t, reasons[0], "spec fingerprint")
	assert.Contains(t, reasons[0], "sha256:captured-elsewhere")
	assert.Contains(t, reasons[0], workerFingerprint)
}

func TestCheckpointCompatReasons_RunscVersionMismatch(t *testing.T) {
	m := matchingManifest()
	m.RunscVersion = "runsc version release-20230101.0"

	reasons := checkpointCompatReasons(m, workerFingerprint, workerVersion)

	assert.Len(t, reasons, 1)
	assert.Contains(t, reasons[0], "runsc")
	assert.Contains(t, reasons[0], "release-20230101.0")
}

func TestCheckpointCompatReasons_BothMismatch(t *testing.T) {
	reasons := checkpointCompatReasons(
		artifact.Manifest{SpecFingerprint: "sha256:other", RunscVersion: "runsc version old"},
		workerFingerprint, workerVersion)
	assert.Len(t, reasons, 2)
}

// A value the worker or the manifest never supplied must not read as a
// mismatch: an empty side is unknown, not different.
func TestCheckpointCompatReasons_MissingValuesAreNotMismatches(t *testing.T) {
	// Worker could not read its runsc version.
	assert.Empty(t, checkpointCompatReasons(matchingManifest(), workerFingerprint, ""))
	// Manifest predates version/fingerprint recording.
	assert.Empty(t, checkpointCompatReasons(artifact.Manifest{}, workerFingerprint, workerVersion))
}

// fakeStore is a checkpointStore that records whether it was drained.
type fakeStore struct {
	ids     []string
	dropped bool
}

func (f *fakeStore) Checkpoints() []string {
	if f.dropped {
		return nil
	}
	return f.ids
}

func (f *fakeStore) DropCheckpoints() { f.dropped = true }

func TestApplyCheckpointCompat_StrictDrops(t *testing.T) {
	store := &fakeStore{ids: []string{"checkpoint_1", "checkpoint_2"}}

	dropped := applyCheckpointCompat(true, len(store.ids), []string{"a reason"}, store, logging.Discard())

	assert.True(t, dropped)
	assert.True(t, store.dropped)
	assert.Empty(t, store.Checkpoints())
}

func TestApplyCheckpointCompat_TolerantKeeps(t *testing.T) {
	store := &fakeStore{ids: []string{"checkpoint_1"}}

	dropped := applyCheckpointCompat(false, len(store.ids), []string{"a reason"}, store, logging.Discard())

	assert.False(t, dropped)
	assert.False(t, store.dropped)
	assert.NotEmpty(t, store.Checkpoints())
}
