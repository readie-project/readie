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
// The previous implementation derived the socket path in one package and the
// container directory in another, from the same root; a single owner keeps
// them from drifting.
type Layout interface {
	// ContainerDir is the host directory bind-mounted into the executor.
	ContainerDir(containerID string) string
	// SocketPath is the executor's unix socket, as seen from the worker.
	SocketPath(containerID string) string
	// CheckpointDir holds checkpoint images.
	CheckpointDir() string
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

// CheckpointDir returns <workerDir>/checkpoints.
func (l DirLayout) CheckpointDir() string {
	return filepath.Join(l.root, config.CheckpointDirName)
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

// NewName returns a fresh, unique container name. Uniqueness matters: the
// previous implementation returned a constant, so two concurrent requests
// collided on both the Docker container name and the host directory.
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
