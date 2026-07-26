// Package artifact models the immutable bundle the offline checkpoint pipeline
// produces and the worker consumes.
//
// There is exactly one root filesystem and one set of checkpoints, and they are
// baked into the worker image together. That is the whole reason this package is
// as small as it is: a checkpoint can only be restored into the filesystem it
// was captured from, and when the two ship in one image they cannot disagree.
//
// Layout on disk:
//
//	<root>/
//	├── rootfs/
//	├── manifest.json
//	└── checkpoints/
//	    └── <checkpointID>/
//	        ├── meta.json
//	        └── …runsc image files…
//
// The pairing used to be enforced by a rootfs_id recorded in every generation
// and every checkpoint. It was never actually compared — only logged — and with
// one rootfs per image there is nothing to compare it against, so it is gone.
// The guarantee moved from a check at load time to being true by construction,
// because the image build is the only way to produce a pair.
//
// Checkpoint IDs are therefore globally unique within a worker. The previous
// layout nested them under generations/<id>/ and needed a "<generation>/
// <checkpoint>" reference to disambiguate; a bare ID is now unambiguous, which
// is also what the router will send once it selects checkpoints.
package artifact

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"time"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/executor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
)

// Well-known names within the artifact tree.
const (
	// RootfsDirName is the one root filesystem every sandbox runs against.
	RootfsDirName = "rootfs"
	// ManifestFileName describes what the checkpoints can be restored into.
	ManifestFileName = "manifest.json"
	// CheckpointsDirName holds one sub-directory per checkpoint.
	CheckpointsDirName = "checkpoints"
	// CheckpointMetaFileName describes a single checkpoint.
	CheckpointMetaFileName = "meta.json"
)

// Sentinel errors.
var (
	// ErrNoArtifacts indicates the artifact root holds no root filesystem.
	//
	// Not fatal at startup: a worker with no rootfs cannot start a sandbox, but
	// it can come up, serve health, register itself as unusable and say why —
	// which is far easier to diagnose than a container that exits before it
	// logs. Rootfs reports this, so the failure lands on the execution that
	// needs a sandbox.
	ErrNoArtifacts = errors.New("no root filesystem found")
	// ErrIncompleteArtifacts indicates checkpoints without a root filesystem to
	// restore them into. Unlike an empty tree this is a real fault: something
	// assembled half an artifact.
	ErrIncompleteArtifacts = errors.New("checkpoints present with no root filesystem")
	// ErrUnknownCheckpoint indicates no checkpoint by that ID is installed.
	ErrUnknownCheckpoint = errors.New("unknown checkpoint")
	// ErrInvalidManifest indicates manifest.json is malformed.
	ErrInvalidManifest = errors.New("invalid manifest")
	// ErrUnsupportedProtocol indicates an executor speaking a wire format this
	// worker does not implement.
	//
	// Refused at load rather than at restore. The failure otherwise happens
	// inside a request, as a dial that succeeds followed by a read that hangs
	// until the execution deadline — which looks like a slow function.
	ErrUnsupportedProtocol = errors.New("unsupported executor protocol")
)

// Manifest describes what the installed checkpoints can be restored into.
//
// Written by the pipeline and treated as immutable by the worker. It carries no
// identity of its own: the worker image tag names the build.
type Manifest struct {
	// RunscVersion is the full `runsc --version` of the binary that captured
	// the checkpoints. gVisor's save format is not stable across releases.
	RunscVersion string `json:"runsc_version"`
	// SpecFingerprint is the semantic hash of the OCI spec the checkpoints were
	// captured under. See the runsc adapter's Fingerprint.
	SpecFingerprint string `json:"spec_fingerprint"`
	// ExecutorArgv is the command the sandbox runs, verbatim. The pipeline
	// records it rather than the worker assembling one, because both must
	// produce the same process or a restore lands in a sandbox running
	// something else.
	ExecutorArgv []string `json:"executor_argv"`
	// ExecutorProtocol is the unix-socket wire format version the executor in
	// the rootfs speaks. Absent means 1, the unframed format that predates the
	// field.
	ExecutorProtocol int `json:"executor_protocol,omitempty"`
	// PythonPath is the in-sandbox PYTHONPATH the rootfs expects.
	PythonPath string `json:"python_path,omitempty"`
	// Overlay and Network are the runtime modes in force at capture time.
	Overlay string `json:"overlay,omitempty"`
	Network string `json:"network,omitempty"`
	// RootReadonly is the root.readonly the checkpoints were captured with.
	RootReadonly bool `json:"root_readonly"`

	CreatedAt time.Time `json:"created_at"`
}

// Protocol is the executor wire format version, defaulting to 1.
//
// A manifest written before the field existed necessarily describes a version-1
// executor, so an absent value is that rather than an error.
func (m Manifest) Protocol() int {
	if m.ExecutorProtocol == 0 {
		return 1
	}
	return m.ExecutorProtocol
}

// Argv is the command the sandbox should run.
func (m Manifest) Argv() []string {
	return append([]string(nil), m.ExecutorArgv...)
}

// Validate reports whether a manifest carries the fields a restore needs.
func (m Manifest) Validate() error {
	var errs []error

	for _, f := range []struct{ name, value string }{
		{"runsc_version", m.RunscVersion},
		{"spec_fingerprint", m.SpecFingerprint},
	} {
		if f.value == "" {
			errs = append(errs, fmt.Errorf("%w: %s is empty", ErrInvalidManifest, f.name))
		}
	}

	if len(m.ExecutorArgv) == 0 {
		errs = append(errs, fmt.Errorf("%w: executor_argv is empty", ErrInvalidManifest))
	}

	if p := m.Protocol(); p != executor.ProtocolVersion {
		errs = append(errs, fmt.Errorf(
			"%w: artifacts speak executor protocol %d, this worker implements %d; "+
				"rebuild the worker image with the current pipeline",
			ErrUnsupportedProtocol, p, executor.ProtocolVersion))
	}

	return errors.Join(errs...)
}

// Checkpoint describes one captured sandbox image.
type Checkpoint struct {
	// ID names the checkpoint and is its directory name. The router sends this
	// value verbatim as checkpoint_id.
	ID string `json:"checkpoint_id"`
	// Producer is "pipeline" or "worker".
	Producer string `json:"producer,omitempty"`
	// RunscVersion and SpecFingerprint must match the sandbox a restore is
	// attempted into. They are duplicated from the manifest so a checkpoint
	// directory is self-describing even in isolation.
	RunscVersion    string `json:"runsc_version,omitempty"`
	SpecFingerprint string `json:"spec_fingerprint,omitempty"`
	// Imports records what the executor pre-imported before being captured.
	// Informational; useful for scheduling and for debugging a cold-looking
	// restore.
	Imports   []string  `json:"imports,omitempty"`
	CreatedAt time.Time `json:"created_at,omitempty"`

	// Dir is the checkpoint's image directory. Populated on load, not
	// serialised.
	Dir string `json:"-"`
}

// Registry is the loaded artifact tree.
type Registry struct {
	root        string
	manifest    Manifest
	hasRootfs   bool
	checkpoints map[string]Checkpoint
}

// Options configures Load.
type Options struct {
	// Root is the artifact directory. Compiled in for the worker binary; tests
	// pass a temporary directory.
	Root string
	Log  *slog.Logger
}

// Load reads the artifact tree.
//
// A malformed checkpoint is logged and skipped rather than failing startup: one
// bad image should not cost a worker every other checkpoint and its rootfs.
//
// Finding nothing is not an error either — see ErrNoArtifacts. Finding
// checkpoints *without* a rootfs is, because that is a half-assembled artifact
// rather than an absent one.
func Load(opts Options) (*Registry, error) {
	log := opts.Log
	if log == nil {
		log = slog.Default()
	}

	registry := &Registry{root: opts.Root, checkpoints: map[string]Checkpoint{}}

	rootfs := filepath.Join(opts.Root, RootfsDirName)
	info, statErr := os.Stat(rootfs)
	registry.hasRootfs = statErr == nil && info.IsDir()

	checkpointDirs, err := os.ReadDir(filepath.Join(opts.Root, CheckpointsDirName))
	switch {
	case errors.Is(err, fs.ErrNotExist):
		checkpointDirs = nil
	case err != nil:
		// A directory that exists but cannot be read is a real fault —
		// permissions, a bad mount — and hiding it would strand a worker with
		// artifacts it should have been able to see.
		return nil, fmt.Errorf("reading %s: %w", filepath.Join(opts.Root, CheckpointsDirName), err)
	}

	if !registry.hasRootfs {
		if len(checkpointDirs) > 0 {
			return nil, fmt.Errorf("%w: %d under %s but %s is missing",
				ErrIncompleteArtifacts, len(checkpointDirs),
				filepath.Join(opts.Root, CheckpointsDirName), rootfs)
		}
		log.Warn("no root filesystem; this worker cannot start sandboxes",
			"path", rootfs)
		return registry, nil
	}

	if err := registry.loadManifest(opts.Root, log); err != nil {
		return nil, err
	}

	for _, entry := range checkpointDirs {
		if !entry.IsDir() {
			continue
		}
		dir := filepath.Join(opts.Root, CheckpointsDirName, entry.Name())
		checkpoint, err := loadCheckpoint(registry.manifest, dir)
		if err != nil {
			log.Warn("skipping unusable checkpoint",
				"checkpoint_id", entry.Name(), logging.KeyError, err)
			continue
		}
		registry.checkpoints[checkpoint.ID] = checkpoint
	}

	log.Info("artifacts loaded",
		"rootfs", rootfs,
		"checkpoints", len(registry.checkpoints),
		"runsc_version", registry.manifest.RunscVersion,
		"executor_protocol", registry.manifest.Protocol())

	return registry, nil
}

// loadManifest reads manifest.json, tolerating its absence.
//
// A rootfs with no manifest still serves cold starts, which is what the worker
// image ships with before any checkpoint has been captured. Validate only runs
// on a manifest that exists, since a missing one describes no checkpoints.
func (r *Registry) loadManifest(root string, log *slog.Logger) error {
	path := filepath.Join(root, ManifestFileName)

	raw, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		log.Warn("no manifest; cold starts only", "path", path)
		return nil
	}
	if err != nil {
		return fmt.Errorf("%w: reading %s: %w", ErrInvalidManifest, ManifestFileName, err)
	}

	var manifest Manifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return fmt.Errorf("%w: parsing %s: %w", ErrInvalidManifest, ManifestFileName, err)
	}
	if err := manifest.Validate(); err != nil {
		return err
	}

	r.manifest = manifest
	return nil
}

// loadCheckpoint reads one checkpoint image directory.
//
// meta.json is optional: a checkpoint produced before the metadata convention
// existed still restores, it simply cannot be pre-verified. Fields absent from
// the file are inherited from the manifest.
func loadCheckpoint(manifest Manifest, dir string) (Checkpoint, error) {
	checkpoint := Checkpoint{
		ID:              filepath.Base(dir),
		RunscVersion:    manifest.RunscVersion,
		SpecFingerprint: manifest.SpecFingerprint,
		Dir:             dir,
	}

	raw, err := os.ReadFile(filepath.Join(dir, CheckpointMetaFileName))
	if errors.Is(err, fs.ErrNotExist) {
		return checkpoint, nil
	}
	if err != nil {
		return Checkpoint{}, fmt.Errorf("reading checkpoint meta %s: %w", dir, err)
	}

	var meta Checkpoint
	if err := json.Unmarshal(raw, &meta); err != nil {
		return Checkpoint{}, fmt.Errorf("parsing checkpoint meta %s: %w", dir, err)
	}

	if meta.ID != "" && meta.ID != checkpoint.ID {
		// The directory name is authoritative: it is how the router addresses
		// the checkpoint, so a meta.json claiming another ID would make lookups
		// inconsistent.
		return Checkpoint{}, fmt.Errorf("checkpoint in %s claims id %q", dir, meta.ID)
	}
	if meta.RunscVersion != "" {
		checkpoint.RunscVersion = meta.RunscVersion
	}
	if meta.SpecFingerprint != "" {
		checkpoint.SpecFingerprint = meta.SpecFingerprint
	}
	checkpoint.Producer = meta.Producer
	checkpoint.Imports = meta.Imports
	checkpoint.CreatedAt = meta.CreatedAt

	return checkpoint, nil
}

// Root returns the artifact root directory.
func (r *Registry) Root() string { return r.root }

// Manifest returns what the installed checkpoints restore into.
func (r *Registry) Manifest() Manifest { return r.manifest }

// Rootfs returns the one root filesystem every sandbox runs against.
//
// It returns ErrNoArtifacts when none is installed. The error rather than an
// empty string is deliberate: an empty path would reach runsc as a relative
// "rootfs" and fail somewhere deep inside a create, with an error naming
// neither the worker nor the missing artifact.
func (r *Registry) Rootfs() (string, error) {
	if !r.hasRootfs {
		return "", fmt.Errorf("%w at %s; rebuild the worker image with a rootfs",
			ErrNoArtifacts, filepath.Join(r.root, RootfsDirName))
	}
	return filepath.Join(r.root, RootfsDirName), nil
}

// Empty reports whether this worker can start a sandbox at all.
//
// The worker uses it to register itself as unusable rather than READY, so the
// router keeps it visible but never places work on it.
func (r *Registry) Empty() bool { return !r.hasRootfs }

// ResolveCheckpoint finds a checkpoint by ID.
func (r *Registry) ResolveCheckpoint(id string) (Checkpoint, error) {
	checkpoint, ok := r.checkpoints[id]
	if !ok {
		return Checkpoint{}, fmt.Errorf("%w: %q is not installed", ErrUnknownCheckpoint, id)
	}
	return checkpoint, nil
}

// Checkpoints returns every installed checkpoint ID, sorted.
func (r *Registry) Checkpoints() []string {
	ids := make([]string, 0, len(r.checkpoints))
	for id := range r.checkpoints {
		ids = append(ids, id)
	}
	// Sorted so logs and tests are stable; map order is not.
	slices.Sort(ids)
	return ids
}

// WriteManifest writes manifest.json into dir.
func WriteManifest(dir string, manifest Manifest) error {
	return writeJSON(filepath.Join(dir, ManifestFileName), manifest)
}

// WriteCheckpointMeta writes meta.json into a checkpoint image directory.
func WriteCheckpointMeta(dir string, checkpoint Checkpoint) error {
	return writeJSON(filepath.Join(dir, CheckpointMetaFileName), checkpoint)
}

// writeJSON writes v atomically, so a reader never observes a partial file.
func writeJSON(path string, v any) error {
	raw, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal %s: %w", filepath.Base(path), err)
	}
	raw = append(raw, '\n')

	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*")
	if err != nil {
		return fmt.Errorf("create temp for %s: %w", path, err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }() // no-op once renamed

	if _, err := tmp.Write(raw); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write %s: %w", tmpName, err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close %s: %w", tmpName, err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename %s to %s: %w", tmpName, path, err)
	}
	return nil
}
