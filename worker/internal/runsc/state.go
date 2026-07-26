package runsc

import (
	"encoding/json"
	"fmt"
	"strings"
)

// ociState is the OCI runtime state document `runsc state` writes to stdout.
//
// Only the fields the worker uses are declared; the rest are ignored.
type ociState struct {
	OCIVersion  string            `json:"ociVersion"`
	ID          string            `json:"id"`
	PID         int               `json:"pid"`
	Status      string            `json:"status"`
	Bundle      string            `json:"bundle"`
	Annotations map[string]string `json:"annotations,omitempty"`
}

// Sandbox lifecycle states, as reported by the runtime.
const (
	stateCreated = "created"
	stateRunning = "running"
	statePaused  = "paused"
	stateStopped = "stopped"
)

// parseState decodes `runsc state` output.
//
// It is a pure function so it can be tested against a fixture captured from a
// real runsc rather than against an invented shape.
func parseState(raw []byte) (ociState, error) {
	var state ociState
	if err := json.Unmarshal(raw, &state); err != nil {
		return ociState{}, fmt.Errorf("parse runsc state: %w", err)
	}
	if state.ID == "" {
		return ociState{}, fmt.Errorf("parse runsc state: no id in %q", truncate(string(raw), 200))
	}
	state.Status = strings.ToLower(strings.TrimSpace(state.Status))
	return state, nil
}

func (s ociState) running() bool { return s.Status == stateRunning || s.Status == statePaused }
func (s ociState) paused() bool  { return s.Status == statePaused }

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
