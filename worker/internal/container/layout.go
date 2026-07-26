// Package container owns the lifecycle of executor containers: acquiring one
// for a request (creating or resuming), releasing it afterwards, and reclaiming
// orphans left behind by a previous process.
package container

import (
	"os"
	"path/filepath"

	"github.com/google/uuid"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
)

// Layout resolves the on-disk paths belonging to a container.
//
// A single owner keeps them from drifting: the socket path and the directory
// it lives in are derived from the same root, and the bundle deliberately sits
// outside the directory the sandbox can see.
type Layout interface {
	// ContainerDir is the host directory bind-mounted into the executor. The
	// executor's socket appears here.
	ContainerDir(containerID string) string
	// SocketPath is the executor's unix socket, as seen from the worker.
	SocketPath(containerID string) string
	// BundleDir holds the generated OCI config and per-sandbox runtime state.
	// It must not be visible inside the sandbox.
	BundleDir(containerID string) string
	// LogPath is where the sandbox's merged output is captured.
	LogPath(containerID string) string
}

// DirLayout lays containers out beneath a single worker directory.
type DirLayout struct {
	root string
}

var _ Layout = DirLayout{}

// NewDirLayout returns a Layout rooted at workerDir.
func NewDirLayout(workerDir string) DirLayout {
	return DirLayout{root: workerDir}
}

// ContainerDir returns <workerDir>/<containerID>.
func (l DirLayout) ContainerDir(containerID string) string {
	return filepath.Join(l.root, containerID)
}

// SocketPath returns <workerDir>/<containerID>/executor.sock.
//
// The executor sees this same file at $EXECUTOR_DIR/executor.sock, because the
// container directory is bind-mounted to that path.
func (l DirLayout) SocketPath(containerID string) string {
	return filepath.Join(l.root, containerID, config.ExecutorSocketName)
}

// BundleDir returns <workerDir>/sandboxes/<containerID>.
//
// Bundles live outside ContainerDir on purpose: that directory is bind-mounted
// into the sandbox, and the sandbox has no business reading its own OCI spec.
func (l DirLayout) BundleDir(containerID string) string {
	return filepath.Join(l.root, config.BundlesDirName, containerID)
}

// LogPath returns the sandbox's log file inside its bundle directory.
func (l DirLayout) LogPath(containerID string) string {
	return filepath.Join(l.BundleDir(containerID), config.SandboxLogName)
}

// BundlesRoot returns the directory holding every bundle.
func (l DirLayout) BundlesRoot() string {
	return filepath.Join(l.root, config.BundlesDirName)
}

// NameGenerator produces container names.
type NameGenerator interface {
	NewName() string
}

// UUIDNamer generates unique names under a fixed prefix.
type UUIDNamer struct {
	prefix string
}

var _ NameGenerator = UUIDNamer{}

// NewUUIDNamer returns a NameGenerator emitting prefix+UUID.
func NewUUIDNamer(prefix string) UUIDNamer {
	return UUIDNamer{prefix: prefix}
}

// NewName returns a fresh, unique container name. Uniqueness matters: two
// concurrent requests must not collide on the runtime's container id or on the
// host directory carrying the executor socket.
func (n UUIDNamer) NewName() string {
	return n.prefix + uuid.NewString()
}

// FS is the filesystem surface the manager needs.
type FS interface {
	MkdirAll(path string, perm os.FileMode) error
	RemoveAll(path string) error
}

// OSFS is an FS backed by the os package.
type OSFS struct{}

var _ FS = OSFS{}

// NewOSFS returns the real filesystem.
func NewOSFS() OSFS { return OSFS{} }

// MkdirAll creates a directory and any missing parents.
func (OSFS) MkdirAll(path string, perm os.FileMode) error { return os.MkdirAll(path, perm) }

// RemoveAll removes a path and everything beneath it.
func (OSFS) RemoveAll(path string) error { return os.RemoveAll(path) }
