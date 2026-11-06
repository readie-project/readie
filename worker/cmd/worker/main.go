// Command worker runs the checkpoint-restore worker node.
//
// It orchestrates executor containers on behalf of the router: accepting
// execution requests over gRPC, streaming them to a Python executor over a
// unix socket, and relaying results back.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/joho/godotenv"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	oteltrace "go.opentelemetry.io/otel/trace"

	"github.com/illinoisdata/readie/worker/internal/app"
	"github.com/illinoisdata/readie/worker/internal/config"
	"github.com/illinoisdata/readie/worker/internal/logging"
)

func main() {
	// All cleanup lives in run: os.Exit skips deferred functions.
	if err := run(); err != nil {
		slog.Error("worker exited with an error", logging.KeyError, err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	loadDotEnv()

	cfg, err := config.Load(os.Getenv)
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}

	log := logging.New(logging.Options{
		Level:  cfg.LogLevel,
		Format: cfg.LogFormat,
		Out:    os.Stdout,
	})

	var tracer oteltrace.Tracer
	shutdownOTel, err := setupOTelSDK(ctx)
	if err != nil {
		log.Warn("failed to set up OpenTelemetry tracing; continuing without it", logging.KeyError, err)
	} else {
		defer func() {
			// The signal context is cancelled by shutdown, but exporting the
			// final spans must still get a bounded chance to finish.
			flushCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
			defer cancel()
			if shutdownErr := shutdownOTel(flushCtx); shutdownErr != nil {
				log.Warn("could not shut down OpenTelemetry tracing", logging.KeyError, shutdownErr)
			}
		}()
		tracer = otel.Tracer("worker")
	}

	worker, err := app.New(ctx, cfg, log, tracer, app.Deps{})
	if err != nil {
		return fmt.Errorf("build worker: %w", err)
	}

	return worker.Run(ctx)
}

// loadDotEnv reads a local .env outside production.
//
// A missing file is not an error: the container image supplies its
// configuration through real environment variables, and the previous
// implementation aborted startup over an absent .env.
func loadDotEnv() {
	if os.Getenv("APP_ENV") == "production" {
		return
	}
	if err := godotenv.Load(); err != nil {
		slog.Debug("no .env file loaded", logging.KeyError, err)
	}
}

// setupOTelSDK bootstraps the OpenTelemetry pipeline.
// If it does not return an error, make sure to call shutdown for proper cleanup.
func setupOTelSDK(ctx context.Context) (func(context.Context) error, error) {
	var shutdownFuncs []func(context.Context) error
	var err error

	// shutdown calls cleanup functions registered via shutdownFuncs.
	// The errors from the calls are joined.
	// Each registered cleanup will be invoked once.
	shutdown := func(ctx context.Context) error {
		var shutdownErr error
		for _, fn := range shutdownFuncs {
			shutdownErr = errors.Join(shutdownErr, fn(ctx))
		}
		shutdownFuncs = nil
		return shutdownErr
	}

	// handleErr calls shutdown for cleanup and makes sure that all errors are returned.
	handleErr := func(inErr error) {
		err = errors.Join(inErr, shutdown(ctx))
	}

	// Set up trace provider.
	traceExporter, err := otlptracegrpc.New(
		ctx, otlptracegrpc.WithInsecure(), // Remove this line if using TLS
		otlptracegrpc.WithEndpoint("telemetry-agent:4317"),
		otlptracegrpc.WithTimeout(5*time.Second),
	)
	if err != nil {
		handleErr(err)
		return shutdown, err
	}

	tracerProvider := sdktrace.NewTracerProvider(sdktrace.WithBatcher(traceExporter))
	shutdownFuncs = append(shutdownFuncs, tracerProvider.Shutdown)
	otel.SetTracerProvider(tracerProvider)

	return shutdown, err
}
