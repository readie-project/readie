package executor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"time"
)

// Telemetry is an untrusted, worker-only JSON trailer, never a client payload.
type Telemetry struct {
	StartTimeUnixNS int64   `json:"start_time_unix_ns"`
	DurationNS      int64   `json:"duration_ns"`
	Outcome         string  `json:"outcome"`
	Phases          []Phase `json:"phases"`
}

// Phase describes one of the executor's seven fixed execution phases.
type Phase struct {
	Name          string `json:"phase"`
	StartOffsetNS int64  `json:"start_offset_ns"`
	DurationNS    int64  `json:"duration_ns"`
	Outcome       string `json:"outcome"`
}

// Trailer limits are independent of the multi-gigabyte opaque result limits.
const (
	MaxTelemetryBytes  = 8192
	TelemetryTimeout   = 100 * time.Millisecond
	TelemetryClockSkew = 5 * time.Second
)

// ErrInvalidTelemetry identifies rejected observations without retaining their contents.
var ErrInvalidTelemetry = errors.New("invalid executor telemetry")

// ReadTelemetry reads only the second message with an absolute deadline and
// checks its smaller limit before allocating. It never returns payload errors.
func (s *Session) ReadTelemetry(ctx context.Context) ([]byte, error) {
	deadline := time.Now().Add(s.opts.TelemetryTimeout)
	if end, ok := ctx.Deadline(); ok && end.Before(deadline) {
		deadline = end
	}
	if err := s.conn.SetReadDeadline(deadline); err != nil {
		return nil, fmt.Errorf("telemetry deadline: %w", err)
	}
	finished := make(chan struct{})
	stop := context.AfterFunc(ctx, func() {
		defer close(finished)
		_ = s.conn.SetReadDeadline(time.Now())
	})
	defer func() {
		if !stop() {
			<-finished
		}
	}()
	var body []byte
	header := make([]byte, LengthBytes)
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if _, err := io.ReadFull(s.conn, header); err != nil {
			return nil, fmt.Errorf("telemetry header: %w", err)
		}
		n, err := DecodeLength(header)
		if err != nil || n > MaxTelemetryBytes-len(body) {
			return nil, ErrInvalidTelemetry
		}
		if n == 0 {
			return body, nil
		}
		offset := len(body)
		body = append(body, make([]byte, n)...)
		if _, err := io.ReadFull(s.conn, body[offset:]); err != nil {
			return nil, fmt.Errorf("telemetry body: %w", err)
		}
	}
}

// object checks required keys, duplicate keys, unknown keys and trailing JSON.
// RawMessage keeps nanoseconds out of floating-point representations.
func object(data []byte, keys ...string) (map[string]json.RawMessage, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	token, err := dec.Token()
	if err != nil || token != json.Delim('{') {
		return nil, ErrInvalidTelemetry
	}
	fields := make(map[string]json.RawMessage, len(keys))
	for dec.More() {
		token, err = dec.Token()
		if err != nil {
			return nil, ErrInvalidTelemetry
		}
		key, ok := token.(string)
		if !ok {
			return nil, ErrInvalidTelemetry
		}
		if _, exists := fields[key]; exists {
			return nil, ErrInvalidTelemetry
		}
		var value json.RawMessage
		if err = dec.Decode(&value); err != nil || bytes.Equal(value, []byte("null")) {
			return nil, ErrInvalidTelemetry
		}
		fields[key] = value
	}
	if _, err = dec.Token(); err != nil {
		return nil, ErrInvalidTelemetry
	}
	if _, err = dec.Token(); !errors.Is(err, io.EOF) {
		return nil, ErrInvalidTelemetry
	}
	if len(fields) != len(keys) {
		return nil, ErrInvalidTelemetry
	}
	for _, key := range keys {
		if _, ok := fields[key]; !ok {
			return nil, ErrInvalidTelemetry
		}
	}
	return fields, nil
}

// ParseTelemetry validates observations against the worker's own attempt window.
// Rejection loses telemetry, not the already delivered execution result.
func ParseTelemetry(data []byte, attemptStart, resultEnd time.Time, maxDuration time.Duration) (Telemetry, error) {
	var t Telemetry
	if len(data) > MaxTelemetryBytes {
		return t, ErrInvalidTelemetry
	}
	fields, err := object(data, "start_time_unix_ns", "duration_ns", "outcome", "phases")
	if err != nil {
		return t, err
	}
	if err = json.Unmarshal(data, &t); err != nil {
		return Telemetry{}, ErrInvalidTelemetry
	}
	if t.StartTimeUnixNS <= 0 || t.DurationNS < 0 || t.DurationNS > int64(maxDuration) ||
		t.StartTimeUnixNS > math.MaxInt64-t.DurationNS ||
		t.StartTimeUnixNS < attemptStart.Add(-TelemetryClockSkew).UnixNano() ||
		t.StartTimeUnixNS > resultEnd.Add(TelemetryClockSkew).UnixNano() {
		return Telemetry{}, ErrInvalidTelemetry
	}
	var phases []json.RawMessage
	if err = json.Unmarshal(fields["phases"], &phases); err != nil || len(phases) == 0 || len(phases) > 7 {
		return Telemetry{}, ErrInvalidTelemetry
	}
	order := map[string]int{"receive": 0, "decode": 1, "dns": 2, "install": 3, "invoke": 4, "serialize": 5, "send": 6}
	previous, end, outcome := -1, int64(0), "success"
	for i, phase := range t.Phases {
		if _, err = object(phases[i], "phase", "start_offset_ns", "duration_ns", "outcome"); err != nil {
			return Telemetry{}, err
		}
		index, ok := order[phase.Name]
		if !ok || index <= previous || phase.StartOffsetNS < end || phase.DurationNS < 0 ||
			phase.StartOffsetNS > t.DurationNS || phase.DurationNS > t.DurationNS-phase.StartOffsetNS ||
			(phase.Outcome != "success" && phase.Outcome != phase.Name+"_error") {
			return Telemetry{}, ErrInvalidTelemetry
		}
		if phase.Outcome != "success" {
			outcome = phase.Outcome
		}
		previous, end = index, phase.StartOffsetNS+phase.DurationNS
	}
	if t.Outcome != outcome {
		return Telemetry{}, ErrInvalidTelemetry
	}
	return t, nil
}
