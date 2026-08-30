// Command ocispec writes an OCI runtime bundle configuration.
//
// It exists so the offline checkpoint pipeline and the worker generate their
// sandbox specifications from the *same* code. A gVisor checkpoint only
// restores into a sandbox whose shape matches the one it was captured from —
// same ordered mount list, same terminal setting, same overlay mode — and two
// independent generators cannot be kept in agreement by review alone. When
// they drift, the failure is an opaque restore error minutes into a request.
//
// Usage:
//
//	ocispec -params params.json          # write <bundle>/config.json
//	ocispec -params params.json -print   # also print the spec fingerprint
//
// The fingerprint is what the worker records alongside a checkpoint and checks
// before attempting a restore.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/runsc"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
)

// params is the input document, mirroring sandbox.CreateSpec in a form that is
// convenient to write from Python.
type params struct {
	ID           string   `json:"id"`
	Annotations  map[string]string `json:"annotations,omitempty"`
	BundleDir    string   `json:"bundle_dir"`
	RootfsPath   string   `json:"rootfs_path"`
	RootReadonly bool     `json:"root_readonly"`
	Args         []string `json:"args"`
	Env          []string `json:"env"`
	Cwd          string   `json:"cwd,omitempty"`
	Mounts       []struct {
		Source      string   `json:"source"`
		Destination string   `json:"destination"`
		Type        string   `json:"type"`
		Options     []string `json:"options"`
	} `json:"mounts,omitempty"`
	MemoryBytes int64  `json:"memory_bytes,omitempty"`
	CPUQuota    int64  `json:"cpu_quota,omitempty"`
	CPUPeriod   int64  `json:"cpu_period,omitempty"`
	PidsLimit   int64  `json:"pids_limit,omitempty"`
	CgroupsPath string `json:"cgroups_path,omitempty"`

	// Overlay, Network and GPU are runtime modes rather than spec fields, but
	// they are part of what a checkpoint is sensitive to, so they belong in the
	// fingerprint.
	Overlay string `json:"overlay,omitempty"`
	Network string `json:"network,omitempty"`
	GPU     bool   `json:"gpu,omitempty"`
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "ocispec: %v\n", err)
		os.Exit(1)
	}
}

// resetFlags re-arms flag parsing so run can be exercised more than once in a
// single process.
func resetFlags() {
	flag.CommandLine = flag.NewFlagSet(os.Args[0], flag.ContinueOnError)
}

func run() error {
	paramsPath := flag.String("params", "", "path to the JSON parameter file")
	out := flag.String("out", "", "config.json path (defaults to <bundle_dir>/config.json)")
	printFingerprint := flag.Bool("print", false, "print the spec fingerprint to stdout")
	flag.Parse()

	if *paramsPath == "" {
		return fmt.Errorf("-params is required")
	}

	raw, err := os.ReadFile(*paramsPath)
	if err != nil {
		return fmt.Errorf("read params: %w", err)
	}

	var p params
	if parseErr := json.Unmarshal(raw, &p); parseErr != nil {
		return fmt.Errorf("parse params: %w", parseErr)
	}

	spec, err := runsc.BuildSpec(toCreateSpec(p))
	if err != nil {
		return fmt.Errorf("build spec: %w", err)
	}

	target := *out
	if target == "" {
		if p.BundleDir == "" {
			return fmt.Errorf("either -out or bundle_dir is required")
		}
		target = filepath.Join(p.BundleDir, "config.json")
	}

	encoded, err := json.MarshalIndent(spec, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal spec: %w", err)
	}
	if err := os.WriteFile(target, append(encoded, '\n'), 0o644); err != nil {
		return fmt.Errorf("write %s: %w", target, err)
	}

	if *printFingerprint {
		fmt.Println(runsc.Fingerprint(spec, p.Overlay, p.Network, p.GPU))
	}
	return nil
}

func toCreateSpec(p params) sandbox.CreateSpec {
	mounts := make([]sandbox.Mount, 0, len(p.Mounts))
	for _, m := range p.Mounts {
		mounts = append(mounts, sandbox.Mount{
			Source:      m.Source,
			Destination: m.Destination,
			Type:        m.Type,
			Options:     m.Options,
		})
	}

	return sandbox.CreateSpec{
		ID:           p.ID,
		Annotations:  p.Annotations,
		BundleDir:    p.BundleDir,
		RootfsPath:   p.RootfsPath,
		RootReadonly: p.RootReadonly,
		Args:         p.Args,
		Env:          p.Env,
		Cwd:          p.Cwd,
		Mounts:       mounts,
		MemoryBytes:  p.MemoryBytes,
		CPUQuota:     p.CPUQuota,
		CPUPeriod:    p.CPUPeriod,
		PidsLimit:    p.PidsLimit,
		CgroupsPath:  p.CgroupsPath,
		GPU:          p.GPU,
	}
}
