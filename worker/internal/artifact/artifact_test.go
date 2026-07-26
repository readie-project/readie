package artifact_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/artifact"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
)

// builder assembles artifact trees on disk the way the pipeline would.
type builder struct {
	t    *testing.T
	root string
}

func newBuilder(t *testing.T) *builder {
	t.Helper()
	return &builder{t: t, root: t.TempDir()}
}

func (b *builder) generation(id string, created time.Time, mutate ...func(*artifact.Generation)) *builder {
	b.t.Helper()

	dir := filepath.Join(b.root, artifact.GenerationsDirName, id)
	require.NoError(b.t, os.MkdirAll(filepath.Join(dir, artifact.RootfsDirName), 0o755))

	gen := artifact.Generation{
		ID:                 id,
		RootfsID:           "sha256:rootfs-" + id,
		RunscVersion:       "runsc version release-20250107.0",
		SpecFingerprint:    "sha256:spec-" + id,
		ExecutorEntrypoint: "/app/executor/app.py",
		Overlay:            "root:memory",
		Network:            "none",
		CreatedAt:          created,
	}
	for _, m := range mutate {
		m(&gen)
	}
	require.NoError(b.t, artifact.WriteGeneration(dir, gen))
	return b
}

func (b *builder) checkpoint(genID, checkpointID string, mutate ...func(*artifact.Checkpoint)) *builder {
	b.t.Helper()

	dir := filepath.Join(b.root, artifact.GenerationsDirName, genID, artifact.CheckpointsDirName, checkpointID)
	require.NoError(b.t, os.MkdirAll(dir, 0o755))
	require.NoError(b.t, os.WriteFile(filepath.Join(dir, "checkpoint.img"), []byte("image"), 0o644))

	meta := artifact.Checkpoint{
		ID:              checkpointID,
		GenerationID:    genID,
		Producer:        "scripts",
		RunscVersion:    "runsc version release-20250107.0",
		SpecFingerprint: "sha256:spec-" + genID,
		RootfsID:        "sha256:rootfs-" + genID,
		Imports:         []string{"pandas", "numpy"},
		CreatedAt:       time.Now().UTC().Truncate(time.Second),
	}
	for _, m := range mutate {
		m(&meta)
	}
	require.NoError(b.t, artifact.WriteCheckpointMeta(dir, meta))
	return b
}

// checkpointWithoutMeta creates an image directory carrying no meta.json.
func (b *builder) checkpointWithoutMeta(genID, checkpointID string) *builder {
	b.t.Helper()

	dir := filepath.Join(b.root, artifact.GenerationsDirName, genID, artifact.CheckpointsDirName, checkpointID)
	require.NoError(b.t, os.MkdirAll(dir, 0o755))
	require.NoError(b.t, os.WriteFile(filepath.Join(dir, "checkpoint.img"), []byte("image"), 0o644))
	return b
}

func (b *builder) load(active string) (*artifact.Registry, error) {
	b.t.Helper()
	return artifact.Load(artifact.Options{Root: b.root, ActiveGeneration: active, Log: logging.Discard()})
}

func (b *builder) mustLoad(active string) *artifact.Registry {
	b.t.Helper()
	r, err := b.load(active)
	require.NoError(b.t, err)
	return r
}

var (
	older = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	newer = time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
)

func TestLoad_IndexesGenerationsAndCheckpoints(t *testing.T) {
	r := newBuilder(t).
		generation("gen-a", older).
		checkpoint("gen-a", "checkpoint_1").
		checkpoint("gen-a", "checkpoint_2").
		mustLoad("")

	gens := r.Generations()
	require.Len(t, gens, 1)
	assert.Equal(t, "gen-a", gens[0].ID)
	assert.Len(t, gens[0].Checkpoints, 2)
	assert.Equal(t, []string{"gen-a/checkpoint_1", "gen-a/checkpoint_2"}, r.CheckpointRefs())
}

func TestLoad_ActiveDefaultsToNewest(t *testing.T) {
	r := newBuilder(t).
		generation("gen-a", older).
		generation("gen-b", newer).
		mustLoad("")

	assert.Equal(t, "gen-b", r.Active().ID)
	assert.Equal(t, []string{"gen-b", "gen-a"}, []string{r.Generations()[0].ID, r.Generations()[1].ID},
		"generations are ordered newest first")
}

func TestLoad_ActiveCanBePinned(t *testing.T) {
	r := newBuilder(t).
		generation("gen-a", older).
		generation("gen-b", newer).
		mustLoad("gen-a")

	assert.Equal(t, "gen-a", r.Active().ID)
}

func TestLoad_PinningAnAbsentGenerationFails(t *testing.T) {
	_, err := newBuilder(t).generation("gen-a", older).load("gen-missing")
	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrUnknownGeneration)
}

// The pipeline names checkpoints checkpoint_1, checkpoint_2, … per run, so the
// same ID exists in every generation. A bare reference must not be ambiguous.
func TestResolveCheckpoint_BareReferenceTakesTheNewestGeneration(t *testing.T) {
	r := newBuilder(t).
		generation("gen-a", older).
		checkpoint("gen-a", "checkpoint_1").
		generation("gen-b", newer).
		checkpoint("gen-b", "checkpoint_1").
		mustLoad("")

	checkpoint, gen, err := r.ResolveCheckpoint("checkpoint_1")
	require.NoError(t, err)

	assert.Equal(t, "gen-b", gen.ID)
	assert.Equal(t, "checkpoint_1", checkpoint.ID)
	assert.Equal(t, "sha256:rootfs-gen-b", checkpoint.RootfsID)
}

func TestResolveCheckpoint_QualifiedReferencePinsAnOlderGeneration(t *testing.T) {
	r := newBuilder(t).
		generation("gen-a", older).
		checkpoint("gen-a", "checkpoint_1").
		generation("gen-b", newer).
		checkpoint("gen-b", "checkpoint_1").
		mustLoad("")

	checkpoint, gen, err := r.ResolveCheckpoint("gen-a/checkpoint_1")
	require.NoError(t, err)

	assert.Equal(t, "gen-a", gen.ID)
	assert.Equal(t, "sha256:rootfs-gen-a", checkpoint.RootfsID)
}

// The rootfs a restore runs against is looked up through the checkpoint, never
// assumed — which is what makes a mismatched pair impossible.
func TestResolveCheckpoint_RootfsComesFromTheOwningGeneration(t *testing.T) {
	b := newBuilder(t).
		generation("gen-a", older).
		checkpoint("gen-a", "checkpoint_1").
		generation("gen-b", newer)
	r := b.mustLoad("")

	require.Equal(t, "gen-b", r.Active().ID, "cold starts use the newest generation")

	_, gen, err := r.ResolveCheckpoint("checkpoint_1")
	require.NoError(t, err)
	assert.Equal(t, "gen-a", gen.ID, "the restore must use the checkpoint's own generation")
	assert.Equal(t,
		filepath.Join(b.root, artifact.GenerationsDirName, "gen-a", artifact.RootfsDirName),
		gen.RootfsPath())
}

func TestResolveCheckpoint_Errors(t *testing.T) {
	r := newBuilder(t).
		generation("gen-a", older).
		checkpoint("gen-a", "checkpoint_1").
		mustLoad("")

	tests := []struct {
		name string
		ref  string
		want error
	}{
		{"empty", "", artifact.ErrUnknownCheckpoint},
		{"unknown bare", "checkpoint_9", artifact.ErrUnknownCheckpoint},
		{"unknown generation", "gen-z/checkpoint_1", artifact.ErrUnknownGeneration},
		{"unknown in known generation", "gen-a/checkpoint_9", artifact.ErrUnknownCheckpoint},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, _, err := r.ResolveCheckpoint(tt.ref)
			require.Error(t, err)
			assert.ErrorIs(t, err, tt.want)
		})
	}
}

// A checkpoint written before the metadata convention existed still restores;
// it simply inherits the generation's identity and cannot be pre-verified.
func TestLoad_CheckpointWithoutMetaInheritsTheGeneration(t *testing.T) {
	r := newBuilder(t).
		generation("gen-a", older).
		checkpointWithoutMeta("gen-a", "checkpoint_1").
		mustLoad("")

	checkpoint, _, err := r.ResolveCheckpoint("checkpoint_1")
	require.NoError(t, err)

	assert.Equal(t, "sha256:rootfs-gen-a", checkpoint.RootfsID)
	assert.Equal(t, "sha256:spec-gen-a", checkpoint.SpecFingerprint)
	assert.Equal(t, "runsc version release-20250107.0", checkpoint.RunscVersion)
}

// One corrupt artifact must not take a worker offline when others are usable.
func TestLoad_SkipsUnusableGenerations(t *testing.T) {
	b := newBuilder(t).generation("gen-good", newer).checkpoint("gen-good", "checkpoint_1")

	// A generation whose rootfs never made it across.
	noRootfs := filepath.Join(b.root, artifact.GenerationsDirName, "gen-no-rootfs")
	require.NoError(t, os.MkdirAll(noRootfs, 0o755))
	require.NoError(t, artifact.WriteGeneration(noRootfs, artifact.Generation{
		ID: "gen-no-rootfs", RootfsID: "x", RunscVersion: "x",
		SpecFingerprint: "x", ExecutorEntrypoint: "/app/executor/app.py",
	}))

	// A generation with unparseable metadata.
	corrupt := filepath.Join(b.root, artifact.GenerationsDirName, "gen-corrupt")
	require.NoError(t, os.MkdirAll(filepath.Join(corrupt, artifact.RootfsDirName), 0o755))
	require.NoError(t, os.WriteFile(filepath.Join(corrupt, artifact.GenerationFileName), []byte("{not json"), 0o644))

	r := b.mustLoad("")

	require.Len(t, r.Generations(), 1)
	assert.Equal(t, "gen-good", r.Generations()[0].ID)
}

// A worker with no rootfs cannot start a sandbox at all, so this must fail
// loudly rather than at the first request.
func TestLoad_FailsWhenNothingIsUsable(t *testing.T) {
	b := newBuilder(t)
	require.NoError(t, os.MkdirAll(filepath.Join(b.root, artifact.GenerationsDirName), 0o755))

	_, err := b.load("")
	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrNoGenerations)
}

func TestLoad_FailsWhenTheArtifactRootIsAbsent(t *testing.T) {
	_, err := artifact.Load(artifact.Options{Root: filepath.Join(t.TempDir(), "nope"), Log: logging.Discard()})
	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrNoGenerations)
}

func TestLoadGeneration_RejectsAnIDThatContradictsItsDirectory(t *testing.T) {
	b := newBuilder(t)
	dir := filepath.Join(b.root, artifact.GenerationsDirName, "gen-a")
	require.NoError(t, os.MkdirAll(filepath.Join(dir, artifact.RootfsDirName), 0o755))
	require.NoError(t, artifact.WriteGeneration(dir, artifact.Generation{
		ID: "gen-somewhere-else", RootfsID: "x", RunscVersion: "x",
		SpecFingerprint: "x", ExecutorEntrypoint: "/app/executor/app.py",
	}))

	_, err := artifact.LoadGeneration(dir)
	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrInvalidGeneration)
}

func TestLoadGeneration_RejectsMissingRequiredFields(t *testing.T) {
	b := newBuilder(t)
	dir := filepath.Join(b.root, artifact.GenerationsDirName, "gen-a")
	require.NoError(t, os.MkdirAll(filepath.Join(dir, artifact.RootfsDirName), 0o755))
	require.NoError(t, os.WriteFile(filepath.Join(dir, artifact.GenerationFileName),
		[]byte(`{"id":"gen-a"}`), 0o644))

	_, err := artifact.LoadGeneration(dir)
	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrInvalidGeneration)
	for _, field := range []string{"rootfs_id", "runsc_version", "spec_fingerprint", "executor_entrypoint"} {
		assert.Contains(t, err.Error(), field)
	}
}

func TestLoad_GenerationWithoutCheckpointsStillServesColdStarts(t *testing.T) {
	r := newBuilder(t).generation("gen-a", older).mustLoad("")

	assert.Equal(t, "gen-a", r.Active().ID)
	assert.Empty(t, r.CheckpointRefs())
	assert.DirExists(t, r.Active().RootfsPath())
}

// Metadata is written atomically so a concurrent reader never sees a partial
// file, and the on-disk form must stay stable for the Python pipeline.
func TestWriteGeneration_ProducesReadableStableJSON(t *testing.T) {
	dir := t.TempDir()
	require.NoError(t, os.MkdirAll(filepath.Join(dir, artifact.RootfsDirName), 0o755))

	want := artifact.Generation{
		ID: filepath.Base(dir), RootfsID: "sha256:abc", RunscVersion: "runsc version x",
		SpecFingerprint: "sha256:def", ExecutorEntrypoint: "/app/executor/app.py",
		Overlay: "root:memory", Network: "none", CreatedAt: older,
	}
	require.NoError(t, artifact.WriteGeneration(dir, want))

	raw, err := os.ReadFile(filepath.Join(dir, artifact.GenerationFileName))
	require.NoError(t, err)

	var got map[string]any
	require.NoError(t, json.Unmarshal(raw, &got))
	assert.Equal(t, "sha256:abc", got["rootfs_id"])
	assert.Equal(t, "/app/executor/app.py", got["executor_entrypoint"])

	loaded, err := artifact.LoadGeneration(dir)
	require.NoError(t, err)
	assert.Equal(t, want.RootfsID, loaded.RootfsID)
	assert.True(t, want.CreatedAt.Equal(loaded.CreatedAt))

	entries, err := os.ReadDir(dir)
	require.NoError(t, err)
	for _, e := range entries {
		assert.NotContains(t, e.Name(), ".tmp", "no temp file may survive an atomic write")
		assert.False(t, len(e.Name()) > 0 && e.Name()[0] == '.', "no dotfile may survive: %s", e.Name())
	}
}

func TestWriteCheckpointMeta_RoundTrips(t *testing.T) {
	b := newBuilder(t).generation("gen-a", older).checkpoint("gen-a", "checkpoint_1", func(c *artifact.Checkpoint) {
		c.Producer = "worker"
		c.Imports = []string{"torch"}
	})
	r := b.mustLoad("")

	checkpoint, _, err := r.ResolveCheckpoint("checkpoint_1")
	require.NoError(t, err)
	assert.Equal(t, "worker", checkpoint.Producer)
	assert.Equal(t, []string{"torch"}, checkpoint.Imports)
	assert.Equal(t, "gen-a", checkpoint.GenerationID)
}

// A checkpoint claiming a different owner than the directory it sits in is a
// packaging error and must not be silently indexed under the wrong rootfs.
func TestLoad_SkipsACheckpointClaimingTheWrongGeneration(t *testing.T) {
	b := newBuilder(t).
		generation("gen-a", older).
		checkpoint("gen-a", "checkpoint_1", func(c *artifact.Checkpoint) { c.GenerationID = "gen-elsewhere" }).
		checkpoint("gen-a", "checkpoint_2")

	r := b.mustLoad("")

	_, _, err := r.ResolveCheckpoint("checkpoint_1")
	assert.ErrorIs(t, err, artifact.ErrUnknownCheckpoint)

	_, _, err = r.ResolveCheckpoint("checkpoint_2")
	assert.NoError(t, err, "a sound checkpoint alongside a bad one stays usable")
}
