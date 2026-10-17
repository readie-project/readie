package execution

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/readie-project/readie/worker/internal/container"
)

// ErrPackageInstall indicates a request's packages could not be installed in
// its sandbox. The installer's output has already gone to the client as logs.
var ErrPackageInstall = errors.New("package install failed")

// installScript runs the executor's DNS bootstrap, then uv. The packages are
// positional parameters ("$@"), never spliced into the script text, so a
// requirement string cannot be read as shell syntax. The "--" ends uv's option
// parsing, so one that begins with "-" cannot be read as a flag either.
//
// The bootstrap comes first because this runs before the executor sees the
// request, and nothing else populates /etc/resolv.conf in a gVisor sandbox.
// It is the executor's own implementation (readie_executor.network), run as a
// module so the logic lives in one place.
//
// uv installs into the first python3 on PATH (--system). The sandbox's PATH starts with
// /usr/local/bin, so that is the image's own Python, the one the executor runs. If PATH ever
// pointed at another interpreter, such as a Debian base's externally managed one, the install
// fails instead of putting packages where the executor never imports from.
const installScript = `python -m readie_executor.network && ` +
	`exec uv pip install --system --no-cache-dir -- "$@"`

// installArgv builds the command that installs specs.
func installArgv(specs []string) []string {
	argv := make([]string, 0, 4+len(specs))
	argv = append(argv, "sh", "-c", installScript, "sh")
	return append(argv, specs...)
}

// installPackages installs a request's packages in the sandbox before the
// executor sees the request. A no-op for an empty list.
//
// The router has already validated, deduped and filtered the list against the
// base image, so everything here is something the image does not carry. The
// install runs every time, including in a reused session container, because
// nothing records what an earlier request installed.
//
// The command's output is sent to the client as logs whether or not it
// succeeded, since uv's message is what explains a bad requirement.
func (r *Runner) installPackages(ctx context.Context, handle container.Handle, specs []string, sink Sink) error {
	if len(specs) == 0 {
		return nil
	}

	result, err := r.containers.Exec(ctx, handle.ID, installArgv(specs))
	if err != nil {
		return fmt.Errorf("%w: %w", ErrPackageInstall, err)
	}
	if len(result.Output) > 0 {
		if logErr := sink.Logs(string(result.Output)); logErr != nil {
			return fmt.Errorf("send install output: %w", logErr)
		}
	}
	if result.ExitCode != 0 {
		return fmt.Errorf("%w: exit %d installing %s",
			ErrPackageInstall, result.ExitCode, strings.Join(specs, " "))
	}
	return nil
}
