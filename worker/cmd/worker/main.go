// Command worker runs the checkpoint-restore worker node.
//
// It orchestrates executor containers on behalf of the router: accepting
// execution requests over gRPC, streaming them to a Python executor over a
// unix socket, and relaying results back.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/joho/godotenv"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/app"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
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

	worker, err := app.New(ctx, cfg, log, app.Deps{})
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
