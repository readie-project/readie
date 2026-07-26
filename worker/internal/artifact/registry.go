package artifact

import (
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
)

// Registry is the worker's view of the generations available on local storage.
//
// It is built once at startup and is read-only thereafter, so it needs no
// locking: swapping in a new generation means restarting the worker, which is
// deliberate — a live swap would strand warm sandboxes on a rootfs the registry
// no longer describes.
type Registry struct {
	root        string
	generations map[string]Generation
	// order lists generation IDs newest first.
	order []string
	// active is the generation cold starts use.
	active string
	// index maps a bare checkpoint ID to the newest generation containing it.
	index map[string]string
}

// Options configures Load.
type Options struct {
	// Root is the artifact directory holding generations/.
	Root string
	// ActiveGeneration pins the generation used for cold starts. Empty selects
	// the newest.
	ActiveGeneration string
	Log              *slog.Logger
}

// Load scans the artifact root and indexes every usable generation.
//
// A generation that fails to load is logged and skipped rather than failing
// startup: one corrupt artifact should not take a worker offline when others
// are serviceable.
//
// Finding nothing at all is not an error either. A worker with no rootfs cannot
// start a sandbox, but it can still come up, serve health, register itself and
// say why it is unusable — which is far easier to diagnose than a container
// that exits before it logs anything. Active reports ErrNoGenerations, so the
// failure lands on the execution that needs a sandbox rather than on startup.
//
// An explicitly requested ActiveGeneration that is not present is still fatal.
// That is a configuration mistake, not a missing artifact, and starting with a
// different generation than the operator asked for would be worse than not
// starting.
func Load(opts Options) (*Registry, error) {
	log := opts.Log
	if log == nil {
		log = slog.Default()
	}

	generationsDir := filepath.Join(opts.Root, GenerationsDirName)

	registry := &Registry{
		root:        opts.Root,
		generations: make(map[string]Generation),
		index:       make(map[string]string),
	}

	entries, err := os.ReadDir(generationsDir)
	if errors.Is(err, fs.ErrNotExist) {
		log.Warn("no artifact directory; this worker cannot start sandboxes until a generation is installed",
			"path", generationsDir)
		return registry, nil
	}
	if err != nil {
		// A directory that exists but cannot be read is a real fault —
		// permissions, a bad mount — and hiding it would strand the worker with
		// artifacts it should have been able to see.
		return nil, fmt.Errorf("reading artifact root %s: %w", generationsDir, err)
	}

	for _, entry := range entries {
		if !entry.IsDir() || strings.HasPrefix(entry.Name(), ".") {
			continue
		}

		dir := filepath.Join(generationsDir, entry.Name())
		gen, err := LoadGeneration(dir)
		if err != nil {
			log.Warn("skipping unusable generation", "generation", entry.Name(), logging.KeyError, err)
			continue
		}

		registry.generations[gen.ID] = gen
		log.Info("loaded generation",
			"generation", gen.ID,
			"rootfs_id", gen.RootfsID,
			"checkpoints", len(gen.Checkpoints),
			"runsc_version", gen.RunscVersion)
	}

	if len(registry.generations) == 0 {
		log.Warn("no usable generation found; this worker cannot start sandboxes",
			"path", generationsDir, "entries", len(entries))
		return registry, nil
	}

	registry.buildOrder()
	if err := registry.selectActive(opts.ActiveGeneration); err != nil {
		return nil, err
	}
	registry.buildIndex(log)

	return registry, nil
}

// buildOrder sorts generations newest first, falling back to descending ID so
// the ordering is total and deterministic even without timestamps.
func (r *Registry) buildOrder() {
	r.order = make([]string, 0, len(r.generations))
	for id := range r.generations {
		r.order = append(r.order, id)
	}

	sort.Slice(r.order, func(i, j int) bool {
		a, b := r.generations[r.order[i]], r.generations[r.order[j]]
		if !a.CreatedAt.Equal(b.CreatedAt) {
			return a.CreatedAt.After(b.CreatedAt)
		}
		return a.ID > b.ID
	})
}

func (r *Registry) selectActive(requested string) error {
	if requested == "" {
		r.active = r.order[0]
		return nil
	}
	if _, ok := r.generations[requested]; !ok {
		return fmt.Errorf("%w: active generation %q is not present", ErrUnknownGeneration, requested)
	}
	r.active = requested
	return nil
}

// buildIndex maps bare checkpoint IDs to a generation.
//
// The pipeline names checkpoints checkpoint_1, checkpoint_2, … per run, so the
// same ID appears in every generation. A bare ID therefore resolves to the
// newest generation containing it; callers needing an older one qualify it as
// "<generation>/<checkpoint>".
func (r *Registry) buildIndex(log *slog.Logger) {
	for _, genID := range r.order { // newest first, so first writer wins
		for checkpointID := range r.generations[genID].Checkpoints {
			if existing, clash := r.index[checkpointID]; clash {
				log.Debug("checkpoint id present in several generations; bare lookups resolve to the newest",
					"checkpoint_id", checkpointID, "resolved_to", existing, "also_in", genID)
				continue
			}
			r.index[checkpointID] = genID
		}
	}
}

// Root returns the artifact root directory.
func (r *Registry) Root() string { return r.root }

// Active returns the generation cold starts use.
//
// It returns ErrNoGenerations when the artifact root held nothing usable. The
// error rather than a zero Generation is deliberate: a zero value has an empty
// Dir, so RootfsPath() would be the relative path "rootfs" and runsc would fail
// somewhere deep inside a create with an error naming neither the worker nor
// the missing artifact.
func (r *Registry) Active() (Generation, error) {
	if r.active == "" {
		return Generation{}, fmt.Errorf(
			"%w under %s; install one and restart the worker",
			ErrNoGenerations, filepath.Join(r.root, GenerationsDirName))
	}
	return r.generations[r.active], nil
}

// Empty reports whether this registry holds no usable generation.
//
// The worker uses it to register itself as unusable rather than READY, so the
// router keeps it visible but never places work on it.
func (r *Registry) Empty() bool { return len(r.generations) == 0 }

// Generations returns every loaded generation, newest first.
func (r *Registry) Generations() []Generation {
	out := make([]Generation, 0, len(r.order))
	for _, id := range r.order {
		out = append(out, r.generations[id])
	}
	return out
}

// Generation returns a generation by ID.
func (r *Registry) Generation(id string) (Generation, error) {
	gen, ok := r.generations[id]
	if !ok {
		return Generation{}, fmt.Errorf("%w: %s", ErrUnknownGeneration, id)
	}
	return gen, nil
}

// ResolveCheckpoint finds a checkpoint and the generation that owns it.
//
// ref is either a bare checkpoint ID, resolving to the newest generation
// containing it, or "<generation>/<checkpoint>" to pin one exactly.
func (r *Registry) ResolveCheckpoint(ref string) (Checkpoint, Generation, error) {
	if ref == "" {
		return Checkpoint{}, Generation{}, fmt.Errorf("%w: empty reference", ErrUnknownCheckpoint)
	}

	genID, checkpointID, qualified := strings.Cut(ref, "/")
	if !qualified {
		checkpointID = ref
		var ok bool
		genID, ok = r.index[checkpointID]
		if !ok {
			return Checkpoint{}, Generation{}, fmt.Errorf("%w: %s", ErrUnknownCheckpoint, ref)
		}
	}

	gen, ok := r.generations[genID]
	if !ok {
		return Checkpoint{}, Generation{}, fmt.Errorf("%w: %s (from reference %q)",
			ErrUnknownGeneration, genID, ref)
	}

	checkpoint, ok := gen.Checkpoints[checkpointID]
	if !ok {
		return Checkpoint{}, Generation{}, fmt.Errorf("%w: %s in generation %s",
			ErrUnknownCheckpoint, checkpointID, genID)
	}
	return checkpoint, gen, nil
}

// CheckpointRefs lists every checkpoint as a fully qualified reference.
func (r *Registry) CheckpointRefs() []string {
	var refs []string
	for _, genID := range r.order {
		for checkpointID := range r.generations[genID].Checkpoints {
			refs = append(refs, genID+"/"+checkpointID)
		}
	}
	sort.Strings(refs)
	return refs
}
