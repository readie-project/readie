package grpcserver

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"

	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/reflection"

	"github.com/illinoisdata/readie/worker/internal/logging"
	pb "github.com/illinoisdata/readie/worker/proto"
)

// ServerOptions configures the gRPC server.
type ServerOptions struct {
	Log *slog.Logger
	// EnableReflection registers the reflection service. The compose
	// healthcheck shells grpcurl, which needs it.
	EnableReflection bool
	// MaxRecvMsgSize and MaxSendMsgSize bound a single message. Payloads are
	// chunked at 1 MiB, so the defaults leave generous headroom.
	MaxRecvMsgSize int
	MaxSendMsgSize int
}

func (o ServerOptions) withDefaults() ServerOptions {
	if o.Log == nil {
		o.Log = slog.Default()
	}
	if o.MaxRecvMsgSize <= 0 {
		o.MaxRecvMsgSize = 16 * 1024 * 1024
	}
	if o.MaxSendMsgSize <= 0 {
		o.MaxSendMsgSize = 16 * 1024 * 1024
	}
	return o
}

// Server owns the gRPC listener, the registered services and their health.
type Server struct {
	grpc     *grpc.Server
	health   *health.Server
	listener net.Listener
	log      *slog.Logger
}

// NewServer registers the execution, health and reflection services.
//
// Health starts as NOT_SERVING. The caller flips it once the worker can
// genuinely serve traffic, so an orchestrator never routes to a worker whose
// dependencies are not ready.
func NewServer(listener net.Listener, svc pb.ExecutionServiceServer, opts ServerOptions) *Server {
	opts = opts.withDefaults()

	grpcServer := grpc.NewServer(
		grpc.MaxRecvMsgSize(opts.MaxRecvMsgSize),
		grpc.MaxSendMsgSize(opts.MaxSendMsgSize),
	)

	pb.RegisterExecutionServiceServer(grpcServer, svc)

	healthServer := health.NewServer()
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	healthpb.RegisterHealthServer(grpcServer, healthServer)

	if opts.EnableReflection {
		reflection.Register(grpcServer)
	}

	return &Server{
		grpc:     grpcServer,
		health:   healthServer,
		listener: listener,
		log:      opts.Log,
	}
}

// Addr reports the address the server is listening on.
func (s *Server) Addr() net.Addr { return s.listener.Addr() }

// Serve blocks until the server stops. A clean stop returns nil.
func (s *Server) Serve() error {
	s.log.Info("gRPC server listening", "addr", s.listener.Addr().String())

	if err := s.grpc.Serve(s.listener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
		return fmt.Errorf("serve gRPC: %w", err)
	}
	return nil
}

// SetServing marks the worker ready or not ready for traffic.
func (s *Server) SetServing(serving bool) {
	status := healthpb.HealthCheckResponse_NOT_SERVING
	if serving {
		status = healthpb.HealthCheckResponse_SERVING
	}
	s.health.SetServingStatus("", status)
	s.log.Info("health status updated", "serving", serving)
}

// GracefulStop drains in-flight streams, falling back to a hard stop if ctx
// expires first.
//
// Draining matters here beyond politeness: each in-flight RequestExecution
// releases its container on the way out, so severing them would strand
// containers that only the next startup sweep would reclaim. The previous
// implementation never stopped the server at all - it simply returned and let
// process exit sever everything.
func (s *Server) GracefulStop(ctx context.Context) error {
	s.health.Shutdown()

	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		s.grpc.GracefulStop()
	}()

	select {
	case <-stopped:
		s.log.Info("gRPC server stopped gracefully")
		return nil

	case <-ctx.Done():
		s.log.Warn("graceful stop timed out; forcing shutdown", logging.KeyError, ctx.Err())
		s.grpc.Stop()
		<-stopped // Stop unblocks GracefulStop, so this cannot hang
		return fmt.Errorf("graceful stop timed out: %w", ctx.Err())
	}
}
