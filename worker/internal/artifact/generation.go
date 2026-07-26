// Package artifact models the immutable bundles the offline checkpoint
// pipeline produces and the worker consumes.
//
// One run of scripts/ emits one generation: a root filesystem plus every
// checkpoint captured against it. Pairing them is not a convenience — a gVisor
// checkpoint can only be restored into the filesystem it was taken from, so a
// checkpoint that travels without its rootfs is unusable, and a worker holding
// a mismatched pair fails opaquely deep inside a restore.
//
// Layout on disk:
//
//	<root>/generations/<generationID>/
//	├── generation.json
//	├── rootfs/
//	└── checkpoints/
//	    └── <checkpointID>/
//	        ├── meta.json
//	        └── …runsc image files…
//
// A worker may hold several generations at once. Each checkpoint resolves to
// the generation that owns it, so the rootfs a restore runs against is looked
// up *through* the checkpoint rather than assumed — which is what makes a
// mismatch impossible rather than merely detected.
package artifact

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

// Well-known names within the artifact tree.
const (
	// GenerationsDirName holds one sub-directory per generation.
	GenerationsDirName = "generations"
	// GenerationFileName describes a generation.
	GenerationFileName = "generation.json"
	// RootfsDirName is the generation's root filesystem.
	RootfsDirName = "rootfs"
	// CheckpointsDirName holds one sub-directory per checkpoint.
	CheckpointsDirName = "checkpoints"
	// CheckpointMetaFileName describes a single checkpoint.
	CheckpointMetaFileName = "meta.json"
)

// Sentinel errors.
var (
	// ErrNoGenerations indicates the artifact root holds nothing usable.
	ErrNoGenerations = errors.New("no usable generations found")
	// ErrUnknownCheckpoint indicates no generation holds the named checkpoint.
	ErrUnknownCheckpoint = errors.New("unknown checkpoint")
	// ErrUnknownGeneration indicates the named generation is not loaded.
	ErrUnknownGeneration = errors.New("unknown generation")
	// ErrInvalidGeneration indicates a generation on disk is malformed.
	ErrInvalidGeneration = errors.New("invalid generation")
)

// Generation describes one artifact produced by a single pipeline run.
//
// It is written by the pipeline as generation.json and treated as immutable by
// the worker.
type Generation struct {
	// ID names the generation and is its directory name.
	ID string `json:"id"`
	// RootfsID identifies the root filesystem's content. Every checkpoint in
	// the generation was captured against exactly this filesystem.
	RootfsID string `json:"rootfs_id"`
	// RunscVersion is the full `runsc --version` of the binary that captured
	// the checkpoints. gVisor's save format is not stable across releases.
	RunscVersion string `json:"runsc_version"`
	// SpecFingerprint is the semantic hash of the OCI spec the checkpoints
	// were captured under. See the runsc adapter's Fingerprint.
	SpecFingerprint string `json:"spec_fingerprint"`
	// ExecutorEntrypoint is the in-sandbox path of the executor program.
	ExecutorEntrypoint string `json:"executor_entrypoint"`
	// PythonPath is the in-sandbox PYTHONPATH the rootfs expects.
	PythonPath string `json:"python_path,omitempty"`
	// Overlay and Network are the runtime modes in force at capture time.
	Overlay string `json:"overlay,omitempty"`
	Network string `json:"network,omitempty"`
	// RootReadonly is the root.readonly the checkpoints were captured with.
	RootReadonly bool `json:"root_readonly"`

	CreatedAt time.Time `json:"created_at"`

	// Dir is the generation's directory. Populated on load, not serialised.
	Dir string `json:"-"`
	// Checkpoints is keyed by checkpoint ID. Populated on load.
	Checkpoints map[string]Checkpoint `json:"-"`
}

// RootfsPath is the generation's root filesystem directory.
func (g Generation) RootfsPath() string { return filepath.Join(g.Dir, RootfsDirName) }

// CheckpointsPath is the generation's checkpoint directory.
func (g Generation) CheckpointsPath() string { return filepath.Join(g.Dir, CheckpointsDirName) }

// Checkpoint describes one captured sandbox image.
type Checkpoint struct {
	// ID names the checkpoint and is its directory name. The router sends
	// this value verbatim as checkpoint_id.
	ID string `json:"checkpoint_id"`
	// GenerationID is the owning generation.
	GenerationID string `json:"generation_id"`
	// Producer is "scripts" or "worker".
	Producer string `json:"producer"`
	// RunscVersion, SpecFingerprint and RootfsID must match the sandbox a
	// restore is attempted into. They are duplicated from the generation so a
	// checkpoint directory is self-describing even in isolation.
	RunscVersion    string `json:"runsc_version"`
	SpecFingerprint string `json:"spec_fingerprint"`
	RootfsID        string `json:"rootfs_id"`
	// Imports records what the executor pre-imported before being captured.
	// Informational; useful for scheduling and for debugging a cold-looking
	// restore.
	Imports []string `json:"imports,omitempty"`

	CreatedAt time.Time `json:"created_at"`

	// Dir is the checkpoint's image directory. Populated on load.
	Dir string `json:"-"`
}

// Validate reports whether a generation carries the fields a restore needs.
func (g Generation) Validate() error {
	var errs []error

	for _, f := range []struct{ name, value string }{
		{"id", g.ID},
		{"rootfs_id", g.RootfsID},
		{"runsc_version", g.RunscVersion},
		{"spec_fingerprint", g.SpecFingerprint},
		{"executor_entrypoint", g.ExecutorEntrypoint},
	} {
		if f.value == "" {
			errs = append(errs, fmt.Errorf("%w: %s is empty", ErrInvalidGeneration, f.name))
		}
	}
	return errors.Join(errs...)
}

// LoadGeneration reads one generation directory.
//
// A generation is only usable if its rootfs is present, so that is checked
// here rather than being discovered at the first restore.
func LoadGeneration(dir string) (Generation, error) {
	raw, err := os.ReadFile(filepath.Join(dir, GenerationFileName))
	if err != nil {
		return Generation{}, fmt.Errorf("%w: reading %s: %w", ErrInvalidGeneration, GenerationFileName, err)
	}

	var gen Generation
	if parseErr := json.Unmarshal(raw, &gen); parseErr != nil {
		return Generation{}, fmt.Errorf("%w: parsing %s: %w", ErrInvalidGeneration, GenerationFileName, parseErr)
	}
	gen.Dir = dir

	// The directory name is authoritative: it is how the generation is
	// addressed, and a generation.json claiming a different id would make
	// lookups inconsistent.
	if name := filepath.Base(dir); gen.ID != "" && gen.ID != name {
		return Generation{}, fmt.Errorf("%w: id %q does not match directory %q",
			ErrInvalidGeneration, gen.ID, name)
	}
	if gen.ID == "" {
		gen.ID = filepath.Base(dir)
	}

	if validateErr := gen.Validate(); validateErr != nil {
		return Generation{}, validateErr
	}

	info, err := os.Stat(gen.RootfsPath())
	if err != nil || !info.IsDir() {
		return Generation{}, fmt.Errorf("%w: rootfs %s is missing or not a directory",
			ErrInvalidGeneration, gen.RootfsPath())
	}

	gen.Checkpoints, err = loadCheckpoints(gen)
	if err != nil {
		return Generation{}, err
	}
	return gen, nil
}

// loadCheckpoints reads every checkpoint belonging to a generation.
//
// An absent checkpoints directory is not an error: a generation with only a
// rootfs still serves cold starts.
func loadCheckpoints(gen Generation) (map[string]Checkpoint, error) {
	entries, err := os.ReadDir(gen.CheckpointsPath())
	if errors.Is(err, fs.ErrNotExist) {
		return map[string]Checkpoint{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("%w: reading checkpoints of %s: %w", ErrInvalidGeneration, gen.ID, err)
	}

	checkpoints := make(map[string]Checkpoint, len(entries))
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		checkpoint, err := loadCheckpoint(gen, filepath.Join(gen.CheckpointsPath(), entry.Name()))
		if err != nil {
			// One malformed checkpoint must not make the generation — and its
			// rootfs — unusable. The caller logs and carries on.
			continue
		}
		checkpoints[checkpoint.ID] = checkpoint
	}
	return checkpoints, nil
}

// loadCheckpoint reads one checkpoint image directory.
//
// meta.json is optional: a checkpoint produced before the metadata convention
// existed still restores, it simply cannot be pre-verified. Fields absent from
// the file are inherited from the generation.
func loadCheckpoint(gen Generation, dir string) (Checkpoint, error) {
	checkpoint := Checkpoint{
		ID:              filepath.Base(dir),
		GenerationID:    gen.ID,
		RunscVersion:    gen.RunscVersion,
		SpecFingerprint: gen.SpecFingerprint,
		RootfsID:        gen.RootfsID,
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

	if meta.ID != "" {
		checkpoint.ID = meta.ID
	}
	if meta.RunscVersion != "" {
		checkpoint.RunscVersion = meta.RunscVersion
	}
	if meta.SpecFingerprint != "" {
		checkpoint.SpecFingerprint = meta.SpecFingerprint
	}
	if meta.RootfsID != "" {
		checkpoint.RootfsID = meta.RootfsID
	}
	if meta.GenerationID != "" && meta.GenerationID != gen.ID {
		return Checkpoint{}, fmt.Errorf("checkpoint %s claims generation %q but lives in %q",
			checkpoint.ID, meta.GenerationID, gen.ID)
	}
	checkpoint.Producer = meta.Producer
	checkpoint.Imports = meta.Imports
	checkpoint.CreatedAt = meta.CreatedAt

	return checkpoint, nil
}

// WriteGeneration writes generation.json into dir.
func WriteGeneration(dir string, gen Generation) error {
	return writeJSON(filepath.Join(dir, GenerationFileName), gen)
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
