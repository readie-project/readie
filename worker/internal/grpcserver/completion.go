package grpcserver

import (
	"context"

	"google.golang.org/grpc/stats"
)

type responseDoneKey struct{}

// completionHandler signals after gRPC writes the final status, not when the
// application handler merely finishes sending payload chunks. It never does I/O.
type completionHandler struct{}

func (completionHandler) TagRPC(ctx context.Context, _ *stats.RPCTagInfo) context.Context {
	return context.WithValue(ctx, responseDoneKey{}, make(chan struct{}))
}

func (completionHandler) HandleRPC(ctx context.Context, event stats.RPCStats) {
	if _, ok := event.(*stats.End); ok {
		if done, ok := ctx.Value(responseDoneKey{}).(chan struct{}); ok {
			close(done)
		}
	}
}

func (completionHandler) TagConn(ctx context.Context, _ *stats.ConnTagInfo) context.Context {
	return ctx
}
func (completionHandler) HandleConn(context.Context, stats.ConnStats) {}
