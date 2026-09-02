package artifact_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/readie/worker/internal/artifact"
	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/logging"
)

// builder lays out an artifact tree the way the worker image bakes one.
type builder struct {
	t    *testing.T
	root string
}

func newBuilder(t *testing.T) *builder {
	t.Helper()
	return &builder{t: t, root: t.TempDir()}
}

// rootfs creates the one root filesystem.
func (b *builder) rootfs() *builder {
	b.t.Helper()
	require.NoError(b.t, os.MkdirAll(filepath.Join(b.root, artifact.RootfsDirName), 0o755))
	return b
}

func validManifest() artifact.Manifest {
	return artifact.Manifest{
		RunscVersion:     "runsc version release-20260721.0",
		SpecFingerprint:  "sha256:spec",
		ExecutorArgv:     []string{"python", "-u", "-m", "readie_executor"},
		ExecutorProtocol: executor.ProtocolVersion,
		PythonPath:       "/lib/python3.12/dist-packages",
		Overlay:          "root:memory",
		Network:          "none",
		CreatedAt:        time.Now().UTC().Truncate(time.Second),
	}
}

// manifest writes manifest.json, optionally mutated.
func (b *builder) manifest(mutate ...func(*artifact.Manifest)) *builder {
	b.t.Helper()
	m := validManifest()
	for _, f := range mutate {
		f(&m)
	}
	require.NoError(b.t, artifact.WriteManifest(b.root, m))
	return b
}

// checkpoint creates one checkpoint image directory with its meta.json.
func (b *builder) checkpoint(id string, imports ...string) *builder {
	b.t.Helper()
	dir := filepath.Join(b.root, artifact.CheckpointsDirName, id)
	require.NoError(b.t, os.MkdirAll(dir, 0o755))
	require.NoError(b.t, os.WriteFile(filepath.Join(dir, "checkpoint.img"), []byte("img"), 0o600))
	require.NoError(b.t, artifact.WriteCheckpointMeta(dir, artifact.Checkpoint{
		ID: id, Producer: "pipeline", Imports: imports,
	}))
	return b
}

// bare creates a checkpoint directory with no meta.json, as a checkpoint
// produced before the metadata convention existed would look.
func (b *builder) bare(id string) *builder {
	b.t.Helper()
	require.NoError(b.t, os.MkdirAll(
		filepath.Join(b.root, artifact.CheckpointsDirName, id), 0o755))
	return b
}

func (b *builder) load() (*artifact.Registry, error) {
	b.t.Helper()
	return artifact.Load(artifact.Options{Root: b.root, Log: logging.Discard()})
}

func (b *builder) mustLoad() *artifact.Registry {
	b.t.Helper()
	r, err := b.load()
	require.NoError(b.t, err)
	return r
}

// ---------------------------------------------------------------------------
// The happy path
// ---------------------------------------------------------------------------
func TestLoad_ReadsARootfsManifestAndCheckpoints(t *testing.T) {
	r := newBuilder(t).rootfs().manifest().
		checkpoint("checkpoint_1", "pandas").checkpoint("checkpoint_2", "torch").mustLoad()

	rootfs, err := r.Rootfs()
	require.NoError(t, err)
	assert.DirExists(t, rootfs)
	assert.Equal(t, []string{"checkpoint_1", "checkpoint_2"}, r.Checkpoints())
	assert.False(t, r.Empty())
}

// A bare ID is unambiguous now that checkpoints are not nested under
// generations. The router sends exactly this.
func TestResolveCheckpoint_FindsACheckpointByBareID(t *testing.T) {
	r := newBuilder(t).rootfs().manifest().checkpoint("checkpoint_1", "pandas", "numpy").mustLoad()

	checkpoint, err := r.ResolveCheckpoint("checkpoint_1")

	require.NoError(t, err)
	assert.Equal(t, "checkpoint_1", checkpoint.ID)
	assert.Equal(t, []string{"pandas", "numpy"}, checkpoint.Imports)
	assert.DirExists(t, checkpoint.Dir)
}

func TestResolveCheckpoint_InheritsCompatibilityFieldsFromTheManifest(t *testing.T) {
	// Duplicated onto the checkpoint so its directory is self-describing even
	// in isolation.
	r := newBuilder(t).rootfs().manifest().checkpoint("checkpoint_1").mustLoad()

	checkpoint, err := r.ResolveCheckpoint("checkpoint_1")

	require.NoError(t, err)
	assert.Equal(t, r.Manifest().RunscVersion, checkpoint.RunscVersion)
	assert.Equal(t, r.Manifest().SpecFingerprint, checkpoint.SpecFingerprint)
}

// DropCheckpoints is how a worker refuses baked checkpoints it cannot restore:
// the rootfs stays, but nothing is offered and every id stops resolving.
func TestDropCheckpoints_LeavesTheWorkerColdOnly(t *testing.T) {
	r := newBuilder(t).rootfs().manifest().
		checkpoint("checkpoint_1").checkpoint("checkpoint_2").mustLoad()
	require.NotEmpty(t, r.Checkpoints())

	r.DropCheckpoints()

	assert.Empty(t, r.Checkpoints())
	_, err := r.ResolveCheckpoint("checkpoint_1")
	assert.ErrorIs(t, err, artifact.ErrUnknownCheckpoint)

	rootfs, rootfsErr := r.Rootfs() // the rootfs survives; only restores are refused
	require.NoError(t, rootfsErr)
	assert.DirExists(t, rootfs)

	r.DropCheckpoints() // idempotent
	assert.Empty(t, r.Checkpoints())
}

func TestResolveCheckpoint_ReportsOneThatIsNotInstalled(t *testing.T) {
	r := newBuilder(t).rootfs().manifest().checkpoint("checkpoint_1").mustLoad()

	_, err := r.ResolveCheckpoint("checkpoint_9")

	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrUnknownCheckpoint)
}

func TestLoad_ACheckpointWithoutMetaStillLoads(t *testing.T) {
	// It restores, it simply cannot be pre-verified.
	r := newBuilder(t).rootfs().manifest().bare("checkpoint_1").mustLoad()

	checkpoint, err := r.ResolveCheckpoint("checkpoint_1")

	require.NoError(t, err)
	assert.Equal(t, "checkpoint_1", checkpoint.ID)
}

func TestLoad_SkipsAMalformedCheckpointAndKeepsTheRest(t *testing.T) {
	// One bad image must not cost the worker every other checkpoint and its
	// rootfs.
	b := newBuilder(t).rootfs().manifest().checkpoint("checkpoint_1")
	broken := filepath.Join(b.root, artifact.CheckpointsDirName, "checkpoint_bad")
	require.NoError(t, os.MkdirAll(broken, 0o755))
	require.NoError(t, os.WriteFile(filepath.Join(broken, "meta.json"), []byte("{"), 0o600))

	assert.Equal(t, []string{"checkpoint_1"}, b.mustLoad().Checkpoints())
}

func TestLoad_SkipsACheckpointClaimingADifferentID(t *testing.T) {
	// The directory name is authoritative: it is how the router addresses the
	// checkpoint, so a meta.json claiming another ID would make lookups
	// inconsistent.
	b := newBuilder(t).rootfs().manifest()
	dir := filepath.Join(b.root, artifact.CheckpointsDirName, "checkpoint_1")
	require.NoError(t, os.MkdirAll(dir, 0o755))
	require.NoError(t, artifact.WriteCheckpointMeta(dir,
		artifact.Checkpoint{ID: "checkpoint_elsewhere"}))

	assert.Empty(t, b.mustLoad().Checkpoints())
}

func TestLoad_IgnoresFilesAmongTheCheckpointDirectories(t *testing.T) {
	b := newBuilder(t).rootfs().manifest().checkpoint("checkpoint_1")
	require.NoError(t, os.WriteFile(
		filepath.Join(b.root, artifact.CheckpointsDirName, "notes.txt"), []byte("x"), 0o600))

	assert.Equal(t, []string{"checkpoint_1"}, b.mustLoad().Checkpoints())
}

func TestCheckpoints_AreSortedSoLogsAndTestsAreStable(t *testing.T) {
	r := newBuilder(t).rootfs().manifest().
		checkpoint("checkpoint_3").checkpoint("checkpoint_1").checkpoint("checkpoint_2").mustLoad()

	assert.Equal(t, []string{"checkpoint_1", "checkpoint_2", "checkpoint_3"}, r.Checkpoints())
}

func TestRoot_ReportsWhereItLoadedFrom(t *testing.T) {
	b := newBuilder(t).rootfs()
	assert.Equal(t, b.root, b.mustLoad().Root())
}

// ---------------------------------------------------------------------------
// Degraded and broken trees
// ---------------------------------------------------------------------------

// A worker with no rootfs cannot start a sandbox, but it can come up, serve
// health and say why it is unusable. That is far easier to diagnose than a
// container which exits before it logs anything.
func TestLoad_SucceedsWithAnEmptyTree(t *testing.T) {
	r := newBuilder(t).mustLoad()

	assert.True(t, r.Empty())
	assert.Empty(t, r.Checkpoints())
}

func TestRootfs_ReportsWhyNothingCanRun(t *testing.T) {
	// An error rather than an empty string: an empty path would reach runsc as
	// a relative "rootfs" and fail deep inside a create, naming neither the
	// worker nor the artifact.
	_, err := newBuilder(t).mustLoad().Rootfs()

	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrNoArtifacts)
	assert.Contains(t, err.Error(), "rebuild the worker image",
		"the message has to say what to do about it")
}

// Checkpoints with no rootfs is a different thing from an empty tree: something
// assembled half an artifact, and restoring any of them is undefined.
func TestLoad_RefusesCheckpointsWithNoRootfs(t *testing.T) {
	_, err := newBuilder(t).manifest().checkpoint("checkpoint_1").load()

	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrIncompleteArtifacts)
	assert.Contains(t, err.Error(), artifact.RootfsDirName,
		"the message names the missing path")
}

func TestLoad_ARootfsWithNoManifestServesColdStartsOnly(t *testing.T) {
	// What the worker image ships with before any checkpoint is captured.
	r := newBuilder(t).rootfs().mustLoad()

	assert.False(t, r.Empty())
	assert.Empty(t, r.Checkpoints())
	_, err := r.Rootfs()
	assert.NoError(t, err)
}

func TestLoad_RefusesAMalformedManifest(t *testing.T) {
	b := newBuilder(t).rootfs()
	require.NoError(t, os.WriteFile(
		filepath.Join(b.root, artifact.ManifestFileName), []byte("{"), 0o600))

	_, err := b.load()

	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrInvalidManifest)
}

func TestLoad_RefusesAManifestMissingWhatARestoreNeeds(t *testing.T) {
	for _, field := range []string{"runsc_version", "spec_fingerprint", "executor_argv"} {
		t.Run(field, func(t *testing.T) {
			b := newBuilder(t).rootfs().manifest(func(m *artifact.Manifest) {
				switch field {
				case "runsc_version":
					m.RunscVersion = ""
				case "spec_fingerprint":
					m.SpecFingerprint = ""
				case "executor_argv":
					m.ExecutorArgv = nil
				}
			})

			_, err := b.load()

			require.Error(t, err)
			assert.ErrorIs(t, err, artifact.ErrInvalidManifest)
			assert.Contains(t, err.Error(), field)
		})
	}
}

// ---------------------------------------------------------------------------
// The executor protocol gate
// ---------------------------------------------------------------------------

// Artifacts whose executor speaks a format this worker does not implement are
// refused at load, not at restore. The failure otherwise happens inside a
// request, as a dial that succeeds followed by a read that hangs to the
// deadline - indistinguishable from a slow function.
func TestProtocol_DefaultsToOneWhenTheFieldIsAbsent(t *testing.T) {
	assert.Equal(t, 1, artifact.Manifest{}.Protocol())
}

func TestLoad_RefusesArtifactsFromAnOlderProtocol(t *testing.T) {
	b := newBuilder(t).rootfs().manifest(func(m *artifact.Manifest) { m.ExecutorProtocol = 0 })

	_, err := b.load()

	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrUnsupportedProtocol)
	assert.Contains(t, err.Error(), "rebuild the worker image",
		"the message has to say what the operator should do about it")
}

func TestLoad_RefusesArtifactsFromAFutureProtocol(t *testing.T) {
	b := newBuilder(t).rootfs().manifest(func(m *artifact.Manifest) {
		m.ExecutorProtocol = executor.ProtocolVersion + 1
	})

	assert.ErrorIs(t, mustLoadErr(t, b), artifact.ErrUnsupportedProtocol)
}

func mustLoadErr(t *testing.T, b *builder) error {
	t.Helper()
	_, err := b.load()
	require.Error(t, err)
	return err
}

// ---------------------------------------------------------------------------
// Argv and serialisation
// ---------------------------------------------------------------------------

// The pipeline records the command and the worker replays it, rather than each
// assembling one - two generators cannot be kept in agreement by review, and a
// disagreement restores a checkpoint into a sandbox running a different process.
func TestArgv_ReturnsTheRecordedCommand(t *testing.T) {
	m := artifact.Manifest{ExecutorArgv: []string{"python", "-u", "-m", "readie_executor"}}

	assert.Equal(t, []string{"python", "-u", "-m", "readie_executor"}, m.Argv())
}

func TestArgv_ReturnsACopySoACallerCannotMutateTheManifest(t *testing.T) {
	m := artifact.Manifest{ExecutorArgv: []string{"python", "-m", "readie_executor"}}

	got := m.Argv()
	got[0] = "sh"

	assert.Equal(t, "python", m.ExecutorArgv[0])
}

func TestWriteManifest_ProducesReadableStableJSON(t *testing.T) {
	// The pipeline writes this file and the worker reads it; the field names are
	// the contract between them.
	dir := t.TempDir()
	want := validManifest()

	require.NoError(t, artifact.WriteManifest(dir, want))

	raw, err := os.ReadFile(filepath.Join(dir, artifact.ManifestFileName))
	require.NoError(t, err)

	var got map[string]any
	require.NoError(t, json.Unmarshal(raw, &got))
	assert.Equal(t, want.RunscVersion, got["runsc_version"])
	assert.Equal(t, want.SpecFingerprint, got["spec_fingerprint"])
	assert.InDelta(t, float64(executor.ProtocolVersion), got["executor_protocol"], 0)
	assert.NotContains(t, got, "id", "identity is the image tag, not a manifest field")
	assert.NotContains(t, got, "rootfs_id", "there is one rootfs, so nothing to identify")
}

func TestWriteManifest_RoundTrips(t *testing.T) {
	b := newBuilder(t).rootfs()
	want := validManifest()
	require.NoError(t, artifact.WriteManifest(b.root, want))

	got := b.mustLoad().Manifest()

	assert.Equal(t, want.RunscVersion, got.RunscVersion)
	assert.Equal(t, want.SpecFingerprint, got.SpecFingerprint)
	assert.Equal(t, want.ExecutorArgv, got.ExecutorArgv)
	assert.Equal(t, want.PythonPath, got.PythonPath)
	assert.True(t, want.CreatedAt.Equal(got.CreatedAt))
}

func TestWriteCheckpointMeta_RoundTrips(t *testing.T) {
	r := newBuilder(t).rootfs().manifest().checkpoint("checkpoint_1", "pandas").mustLoad()

	checkpoint, err := r.ResolveCheckpoint("checkpoint_1")

	require.NoError(t, err)
	assert.Equal(t, "pipeline", checkpoint.Producer)
	assert.Equal(t, []string{"pandas"}, checkpoint.Imports)
}
