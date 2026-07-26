// Package testutil holds helpers shared across the worker's tests.
package testutil

import (
	"runtime"
	"testing"
	"time"
)

// AssertNoLeak fails the test if goroutines outlive it.
//
// It snapshots the goroutine count now and, at cleanup, polls until the count
// settles back. Polling rather than sampling once avoids flaking on goroutines
// that are already on their way out.
func AssertNoLeak(t *testing.T) {
	t.Helper()

	before := runtime.NumGoroutine()

	t.Cleanup(func() {
		deadline := time.Now().Add(2 * time.Second)
		for {
			runtime.Gosched()
			after := runtime.NumGoroutine()
			if after <= before {
				return
			}
			if time.Now().After(deadline) {
				t.Errorf("goroutine leak: %d before, %d after", before, after)
				return
			}
			time.Sleep(10 * time.Millisecond)
		}
	})
}
