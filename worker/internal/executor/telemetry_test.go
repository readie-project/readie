package executor_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/readie/worker/internal/executor"
)

func TestTelemetry_RejectsInvalidReports(t *testing.T) {
	start := time.Now()
	data, err := json.Marshal(executor.Telemetry{
		StartTimeUnixNS: start.UnixNano(), DurationNS: 100, Outcome: "success",
		Phases: []executor.Phase{{Name: "receive", DurationNS: 100, Outcome: "success"}},
	})
	require.NoError(t, err)
	valid := string(data)
	phase := `{"phase":"receive","start_offset_ns":0,"duration_ns":100,"outcome":"success"}`
	cases := map[string]string{
		"malformed":              "{",
		"missing field":          strings.Replace(valid, `"duration_ns":100,`, "", 1),
		"duplicate field":        strings.Replace(valid, `"duration_ns":100`, `"duration_ns":100,"duration_ns":100`, 1),
		"fractional timing":      strings.Replace(valid, `"duration_ns":100`, `"duration_ns":1.5`, 1),
		"integer overflow":       strings.Replace(valid, `"duration_ns":100`, `"duration_ns":9223372036854775808`, 1),
		"excessive duration":     strings.Replace(valid, `"duration_ns":100`, fmt.Sprintf(`"duration_ns":%d`, int64(time.Hour)+1), 1),
		"phase outside duration": strings.Replace(valid, `"start_offset_ns":0`, `"start_offset_ns":101`, 1),
		"repeated phase":         strings.Replace(valid, phase, phase+","+phase, 1),
		"unknown phase":          strings.ReplaceAll(valid, "receive", "private-phase"),
		"inconsistent outcome":   strings.Replace(valid, `"outcome":"success"`, `"outcome":"invoke_error"`, 1),
		"clock skew":             strings.Replace(valid, fmt.Sprint(start.UnixNano()), "1", 1),
	}
	for name, report := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := executor.ParseTelemetry([]byte(report), start, start, time.Hour)
			require.ErrorIs(t, err, executor.ErrInvalidTelemetry)
		})
	}
}

func TestTelemetry_BoundsReading(t *testing.T) {
	for _, name := range []string{"oversized", "truncated", "stalled"} {
		t.Run(name, func(t *testing.T) {
			client, server := net.Pipe()
			t.Cleanup(func() { _ = client.Close(); _ = server.Close() })
			session := newSession(t, client, executor.SessionOptions{TelemetryTimeout: 10 * time.Millisecond})
			go func() {
				switch name {
				case "oversized":
					_, _ = server.Write(executor.EncodeLength(executor.MaxTelemetryBytes + 1))
				case "truncated":
					_, _ = server.Write(executor.EncodeLength(10))
					_ = server.Close()
				}
			}()
			_, err := session.ReadTelemetry(context.Background())
			require.Error(t, err)
		})
	}
}
