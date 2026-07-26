package runsc

import (
	"io"
	"os"
	"sync"
	"time"
)

// defaultTailPoll is how often a following reader re-checks a quiet file.
//
// Polling rather than watching keeps this dependency-free and behaves
// identically on darwin and linux, which matters because this is one of the
// few runtime paths that can be exercised on a development machine.
const defaultTailPoll = 100 * time.Millisecond

// tailReader reads a sandbox's log file, optionally following it as it grows.
//
// The sandbox writes to this file through an inherited descriptor for its
// whole life, so a plain reader would hit EOF the instant it caught up. Close
// must unblock a pending Read: the execution runner cancels log streaming with
// context.AfterFunc(ctx, func() { logs.Close() }) and would otherwise leak the
// pump goroutine for the rest of the execution.
type tailReader struct {
	file   *os.File
	follow bool
	poll   time.Duration

	closeOnce sync.Once
	done      chan struct{}
}

var _ io.ReadCloser = (*tailReader)(nil)

func newTailReader(f *os.File, follow bool, poll time.Duration) *tailReader {
	if poll <= 0 {
		poll = defaultTailPoll
	}
	return &tailReader{file: f, follow: follow, poll: poll, done: make(chan struct{})}
}

// Read returns the next bytes, waiting for more when following.
func (t *tailReader) Read(p []byte) (int, error) {
	for {
		select {
		case <-t.done:
			return 0, io.EOF
		default:
		}

		n, err := t.file.Read(p)
		if n > 0 {
			return n, nil
		}
		if err != nil && err != io.EOF {
			// A read on a closed file after Close is a clean end, not a fault.
			select {
			case <-t.done:
				return 0, io.EOF
			default:
			}
			return 0, err
		}
		if err == io.EOF && !t.follow {
			return 0, io.EOF
		}

		// Caught up with a file that is still being written. Wait, but stay
		// interruptible.
		timer := time.NewTimer(t.poll)
		select {
		case <-timer.C:
		case <-t.done:
			timer.Stop()
			return 0, io.EOF
		}
	}
}

// Close stops following and releases the file. It is idempotent.
func (t *tailReader) Close() error {
	var err error
	t.closeOnce.Do(func() {
		close(t.done)
		err = t.file.Close()
	})
	return err
}
