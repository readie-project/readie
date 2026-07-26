package runsc

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"path"
	"sort"
	"strings"

	specs "github.com/opencontainers/runtime-spec/specs-go"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
)

// ociVersion is the runtime-spec version written into every bundle.
const ociVersion = "1.0.2-dev"

// devShmSize is the size of the sandbox's /dev/shm.
//
// The OCI default of 64 MiB is too small for anything using multiprocessing,
// torch dataloaders or large shared numpy arrays, all of which are plausible
// in this rootfs. Note this diverges from `runsc spec` defaults, so it is part
// of the fingerprint and the offline pipeline must match it.
const devShmSize = "1024m"

// defaultPath is the sandbox's PATH.
//
// The rootfs binary directory is prepended, mirroring what the pipeline
// does to make `python` resolvable.
const defaultPath = "PATH=/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

// BuildSpec produces the OCI runtime spec for a sandbox.
//
// Every field here is part of a contract. The executor finds its socket
// through EXECUTOR_DIR and its imports through PYTHONPATH; the /tmp bind is
// what makes that socket visible to the worker; and the whole shape must match
// the sandbox a checkpoint was captured from or a restore fails. Change
// nothing here without regenerating checkpoints.
func BuildSpec(spec sandbox.CreateSpec) (*specs.Spec, error) {
	if err := validateSpec(spec); err != nil {
		return nil, err
	}

	env := append([]string{defaultPath, "TERM=xterm", "HOME=/root", "PYTHONUNBUFFERED=1"}, spec.Env...)

	out := &specs.Spec{
		Version: ociVersion,
		Process: &specs.Process{
			// A pty would merge stdout and stderr onto a console socket and
			// require --console-socket at create time. Passing plain
			// descriptors is what makes log capture possible at all, and the
			// tty-ness must match between capture and restore.
			Terminal: false,
			User:     specs.User{UID: 0, GID: 0},
			Args:     spec.Args,
			Env:      env,
			Cwd:      cwdOr(spec.Cwd),
			Capabilities: &specs.LinuxCapabilities{
				Bounding:  defaultCapabilities(),
				Effective: defaultCapabilities(),
				Permitted: defaultCapabilities(),
			},
			Rlimits:         []specs.POSIXRlimit{{Type: "RLIMIT_NOFILE", Hard: 4096, Soft: 4096}},
			NoNewPrivileges: true,
		},
		Root: &specs.Root{
			Path:     spec.RootfsPath,
			Readonly: spec.RootReadonly,
		},
		Hostname: "executor",
		Mounts:   buildMounts(spec.Mounts),
		Linux: &specs.Linux{
			CgroupsPath: spec.CgroupsPath,
			Resources:   buildResources(spec),
			Namespaces: []specs.LinuxNamespace{
				{Type: specs.PIDNamespace},
				// Retained but inert: gVisor takes its networking mode from
				// the global --network flag, not from the spec. Removing it
				// would change the fingerprint for no benefit.
				{Type: specs.NetworkNamespace},
				{Type: specs.IPCNamespace},
				{Type: specs.UTSNamespace},
				{Type: specs.MountNamespace},
			},
		},
	}
	return out, nil
}

func cwdOr(cwd string) string {
	if cwd == "" {
		return "/"
	}
	return cwd
}

func defaultCapabilities() []string {
	return []string{"CAP_AUDIT_WRITE", "CAP_KILL", "CAP_NET_BIND_SERVICE"}
}

// buildMounts returns the standard mount set followed by the caller's binds.
//
// Order is significant to the runtime and to the fingerprint, so the standard
// entries always come first and the caller's are appended in the order given.
func buildMounts(extra []sandbox.Mount) []specs.Mount {
	mounts := []specs.Mount{
		{Destination: "/proc", Type: "proc", Source: "proc", Options: []string{}},
		{Destination: "/dev", Type: "tmpfs", Source: "tmpfs",
			Options: []string{"nosuid", "strictatime", "mode=755", "size=65536k"}},
		{Destination: "/dev/pts", Type: "devpts", Source: "devpts",
			Options: []string{"nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620", "gid=5"}},
		{Destination: "/dev/shm", Type: "tmpfs", Source: "shm",
			Options: []string{"nosuid", "noexec", "nodev", "mode=1777", "size=" + devShmSize}},
		{Destination: "/dev/mqueue", Type: "mqueue", Source: "mqueue",
			Options: []string{"nosuid", "noexec", "nodev"}},
		{Destination: "/sys", Type: "sysfs", Source: "sysfs",
			Options: []string{"nosuid", "noexec", "nodev", "ro"}},
	}

	for _, m := range extra {
		mounts = append(mounts, specs.Mount{
			Destination: m.Destination,
			Type:        m.Type,
			Source:      m.Source,
			Options:     m.Options,
		})
	}
	return mounts
}

// buildResources renders the router's allocation as cgroup limits.
//
// They are always written, even when the runtime is told to ignore cgroups, so
// the fingerprint does not depend on whether cgroup delegation happens to be
// available and the limits apply the moment it is.
func buildResources(spec sandbox.CreateSpec) *specs.LinuxResources {
	resources := &specs.LinuxResources{
		Devices: []specs.LinuxDeviceCgroup{{Allow: false, Access: "rwm"}},
	}

	if spec.MemoryBytes > 0 {
		limit := spec.MemoryBytes
		resources.Memory = &specs.LinuxMemory{Limit: &limit}
	}
	if spec.CPUQuota > 0 {
		quota := spec.CPUQuota
		period := uint64(spec.CPUPeriod)
		if period == 0 {
			period = 100000
		}
		resources.CPU = &specs.LinuxCPU{Quota: &quota, Period: &period}
	}
	if spec.PidsLimit > 0 {
		pids := spec.PidsLimit
		resources.Pids = &specs.LinuxPids{Limit: &pids}
	}
	return resources
}

// validateSpec rejects specs the runtime would reject less legibly.
func validateSpec(spec sandbox.CreateSpec) error {
	switch {
	case spec.ID == "":
		return fmt.Errorf("%w: id is empty", sandbox.ErrInvalidSpec)
	case spec.RootfsPath == "":
		return fmt.Errorf("%w: rootfs path is empty", sandbox.ErrInvalidSpec)
	case !path.IsAbs(spec.RootfsPath):
		return fmt.Errorf("%w: rootfs path %q must be absolute", sandbox.ErrInvalidSpec, spec.RootfsPath)
	case len(spec.Args) == 0:
		return fmt.Errorf("%w: args are empty", sandbox.ErrInvalidSpec)
	}

	seen := make(map[string]bool, len(spec.Mounts))
	for i, m := range spec.Mounts {
		if m.Destination == "" || !path.IsAbs(m.Destination) {
			return fmt.Errorf("%w: mount %d destination %q must be an absolute path",
				sandbox.ErrInvalidSpec, i, m.Destination)
		}
		if seen[m.Destination] {
			return fmt.Errorf("%w: mount destination %q appears twice",
				sandbox.ErrInvalidSpec, m.Destination)
		}
		seen[m.Destination] = true

		if m.Type == "bind" && (m.Source == "" || !path.IsAbs(m.Source)) {
			return fmt.Errorf("%w: bind mount %q needs an absolute source, got %q",
				sandbox.ErrInvalidSpec, m.Destination, m.Source)
		}
		// A child mounted before its parent is shadowed when the parent is
		// mounted over it, which presents as a mysteriously empty directory.
		for j := i + 1; j < len(spec.Mounts); j++ {
			if isAncestor(spec.Mounts[j].Destination, m.Destination) {
				return fmt.Errorf("%w: mount %q must be listed before its child %q",
					sandbox.ErrInvalidSpec, spec.Mounts[j].Destination, m.Destination)
			}
		}
	}
	return nil
}

// isAncestor reports whether parent strictly contains child.
func isAncestor(parent, child string) bool {
	parent = path.Clean(parent)
	child = path.Clean(child)
	if parent == child {
		return false
	}
	if parent == "/" {
		return true
	}
	return strings.HasPrefix(child, parent+"/")
}

// Fingerprint hashes the parts of a sandbox a checkpoint is sensitive to.
//
// It deliberately ignores the marshalled JSON: runtime-spec upgrades reorder
// and add fields, and the memory limit varies per request. What must agree
// between the sandbox a checkpoint was captured from and the one it is
// restored into is the *shape* of the sandbox — the program, the mount table,
// the namespaces, and the runtime modes — not the bytes.
//
// Excluded on purpose: every mount source (resolved fresh from the bundle at
// restore time, which is what makes a checkpoint portable between workers),
// the memory limit, the cgroups path, the hostname and the environment.
func Fingerprint(spec *specs.Spec, overlay, network string) string {
	var b strings.Builder

	if spec.Process != nil {
		fmt.Fprintf(&b, "args=%s\n", strings.Join(spec.Process.Args, "\x00"))
		fmt.Fprintf(&b, "terminal=%t\n", spec.Process.Terminal)
		fmt.Fprintf(&b, "cwd=%s\n", spec.Process.Cwd)
	}
	if spec.Root != nil {
		fmt.Fprintf(&b, "root.readonly=%t\n", spec.Root.Readonly)
	}

	for _, m := range spec.Mounts {
		options := append([]string(nil), m.Options...)
		sort.Strings(options)
		fmt.Fprintf(&b, "mount=%s|%s|%s\n", m.Destination, m.Type, strings.Join(options, ","))
	}

	if spec.Linux != nil {
		for _, ns := range spec.Linux.Namespaces {
			fmt.Fprintf(&b, "ns=%s\n", ns.Type)
		}
		if r := spec.Linux.Resources; r != nil {
			if r.CPU != nil {
				fmt.Fprintf(&b, "cpu.quota=%v\ncpu.period=%v\n", deref(r.CPU.Quota), deref(r.CPU.Period))
			}
			if r.Pids != nil {
				fmt.Fprintf(&b, "pids.limit=%v\n", deref(r.Pids.Limit))
			}
		}
	}

	fmt.Fprintf(&b, "overlay=%s\nnetwork=%s\n", overlay, network)

	sum := sha256.Sum256([]byte(b.String()))
	return "sha256:" + hex.EncodeToString(sum[:])
}

func deref[T any](p *T) any {
	if p == nil {
		return "nil"
	}
	return *p
}
