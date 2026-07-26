package executor

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"time"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/clock"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
)

// Dialer opens a connection to a container's executor.
type Dialer interface {
	Dial(ctx context.Context, containerID string) (Conn, error)
}

// PathResolver maps a container id to its socket path. container.DirLayout
// satisfies it.
type PathResolver interface {
	SocketPath(containerID string) string
}

// RetryPolicy bounds how long a dial may keep retrying.
type RetryPolicy struct {
	// Total is the overall budget. It must exceed the 30 seconds the executor
	// spends awaiting a checkpoint before binding its socket, or a cold
	// container can never be reached.
	Total time.Duration
	// Interval is the pause between attempts.
	Interval time.Duration
	// AttemptTimeout bounds a single dial.
	AttemptTimeout time.Duration
}

// UnixDialer connects to executors over unix domain sockets.
//
// It holds no connection state. The previous implementation cached connections
// in an unsynchronised package-level map, which both raced and returned dead
// sockets: the executor closes the connection after each response and loops
// back to accept, so a cached entry is never reusable.
type UnixDialer struct {
	paths PathResolver
	retry RetryPolicy
	clk   clock.Clock
	log   *slog.Logger
}

var _ Dialer = (*UnixDialer)(nil)

// NewUnixDialer returns a Dialer for executor sockets.
func NewUnixDialer(paths PathResolver, retry RetryPolicy, clk clock.Clock, log *slog.Logger) *UnixDialer {
	if clk == nil {
		clk = clock.NewSystem()
	}
	if log == nil {
		log = slog.Default()
	}
	if retry.Interval <= 0 {
		retry.Interval = 100 * time.Millisecond
	}
	if retry.AttemptTimeout <= 0 {
		retry.AttemptTimeout = time.Second
	}
	return &UnixDialer{paths: paths, retry: retry, clk: clk, log: log}
}

// Dial connects to a container's executor, retrying until the socket accepts,
// the budget expires, or ctx is cancelled.
func (d *UnixDialer) Dial(ctx context.Context, containerID string) (Conn, error) {
	path := d.paths.SocketPath(containerID)
	log := d.log.With(logging.KeyContainerID, containerID, "socket", path)

	// budgetCtx bounds the retry loop. The parent is kept separate so an
	// exhausted budget (ErrDialTimeout) stays distinguishable from a caller
	// that gave up (the parent's own error).
	budgetCtx, cancel := context.WithTimeout(ctx, d.retry.Total)
	defer cancel()

	var dialer net.Dialer
	attempts := 0

	giveUp := func() error {
		if err := ctx.Err(); err != nil {
			return fmt.Errorf("dial executor socket %s after %d attempts: %w", path, attempts, err)
		}
		return fmt.Errorf("dial executor socket %s after %d attempts in %s: %w",
			path, attempts, d.retry.Total, ErrDialTimeout)
	}

	for {
		attempts++

		attemptCtx, attemptCancel := context.WithTimeout(budgetCtx, d.retry.AttemptTimeout)
		conn, err := dialer.DialContext(attemptCtx, "unix", path)
		attemptCancel()

		if err == nil {
			log.Debug("connected to executor socket", "attempts", attempts)
			return conn, nil
		}

		// A refused or missing socket is expected while the executor is still
		// starting up, so only the overall budget ends the loop.
		if budgetCtx.Err() != nil {
			return nil, giveUp()
		}
		if !d.clk.Sleep(budgetCtx, d.retry.Interval) {
			return nil, giveUp()
		}
	}
}
