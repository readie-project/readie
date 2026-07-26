# The worker: a Go binary that drives runsc, on a base carrying the root
# filesystem, with the captured checkpoints baked in beside it.
#
# One image is one deployable. The previous split — rootfs and checkpoints as a
# separate artifact bind-mounted at runtime — meant three manual steps between
# capturing a checkpoint and a worker being able to restore it, each of which
# could be got wrong silently.
#
# The 26 GB rootfs lives in crfs-worker-base (see Dockerfile.base) and is
# *inherited* rather than copied, so every generation shares that layer by
# digest and this build takes seconds. Build with `make generation`, which
# captures checkpoints first — `docker build` cannot, since runsc needs
# privileged namespace access a stock BuildKit builder will not grant.
#
# The build context is the repository root: this needs the pipeline's output,
# which does not live under worker/.
FROM golang:1.25-bookworm AS go-builder

WORKDIR /app

# Dependencies first, so the module cache layer survives source edits.
COPY worker/go.mod worker/go.sum ./
RUN go mod download

COPY worker/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o go-server ./cmd/worker
# ocispec generates OCI bundles from the same code the worker uses. The offline
# checkpoint pipeline runs it, which is what keeps the two in agreement.
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o ocispec ./cmd/ocispec

FROM crfs-worker-base:latest
WORKDIR /app

COPY --from=go-builder /app/go-server /app/go-server
COPY --from=go-builder /app/ocispec   /usr/local/bin/ocispec

# manifest.json and checkpoints/, produced by `crfs-pipeline build`.
#
# Empty by default — worker/artifacts/ holds only a .gitkeep — so
# `docker compose build` still yields a working worker: rootfs, no checkpoints,
# every start cold. `make generation` writes captured checkpoints here first.
ARG ARTIFACTS_DIR=worker/artifacts
COPY ${ARTIFACTS_DIR}/ /var/lib/crfs/

ENV SERVICE_NAME=worker
ENV PORT=50052
ENV WORKER_DIR=/shared
ENV ROUTER_URI=router:50051
ENV RUNSC_BINARY=/usr/local/bin/runsc
ENV RUNSC_ROOT=/run/crfs-runsc
ENV APP_ENV=production
ENV LOG_LEVEL=info
ENV LOG_FORMAT=json

EXPOSE 50052

CMD ["/app/go-server"]
