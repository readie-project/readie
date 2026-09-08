# The worker: a Go server that drives runsc, on a base that carries everything a
# checkpoint's validity is bound to.
#
# The split is along that line. `readie-worker-base` - built by
# ../pipeline/Dockerfile - holds the pinned runsc, the root filesystem, and the
# checkpoints captured against it. This file adds only what is independent of all
# of that: the Go binary and grpcurl. So a worker code change rebuilds a Go binary
# and two small layers on top of an image it already has, rather than anything
# involving 26 GB, and `./worker` is the whole build context.
#
#   make worker-image        # from the repository root
#   docker compose up -d     # recreates the worker on the new image
#
# The 26 GB rootfs layer is *inherited* from the base, never copied here. A layer
# inherited from a base image has the same digest by definition, so successive
# worker builds share it and a push carries only the binary.
#
# Declared before the first FROM so it can be used in one. Overridable so CI can
# build this file against a stub base - it needs no rootfs to prove the image
# assembles - and so a deployment can pull a published base by digest.
ARG BASE_IMAGE=readie-worker-base:latest

FROM golang:1.25-bookworm AS go-builder

WORKDIR /app

# Dependencies first, so the module cache layer survives source edits.
COPY go.mod go.sum ./
RUN go mod download

COPY . ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o go-server ./cmd/worker

# ocispec is deliberately not built here. It generates OCI bundles from the same
# code the worker uses, but only the offline pipeline ever runs it, so it belongs
# in the pipeline image and not in this one.

# grpcurl backs the compose healthcheck. The archive is architecture-specific, so
# derive it rather than pinning one.
FROM debian:bookworm-slim AS grpcurl
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) GRPCURL_ARCH=x86_64 ;; \
        arm64) GRPCURL_ARCH=arm64  ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && wget -qO- "https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_${GRPCURL_ARCH}.tar.gz" \
       | tar -xz -C /usr/local/bin grpcurl

# The deployable. runsc, the rootfs and the checkpoints all arrive with the base;
# nothing is mounted. There is deliberately no `RUN runsc --version` here: the
# base verified it at its own build, and requiring it would stop this file being
# buildable against a stub.
FROM ${BASE_IMAGE}

# iproute2 (ip) and iptables build and tear down each sandbox's own network
# namespace and veth pair, and install the shared host NAT/forwarding rules,
# when SANDBOX_NETWORK_PROVISION is set - see internal/network. Absent from
# the base chain otherwise, so this worker adds them itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 iptables \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=go-builder /app/go-server      /app/go-server
COPY --from=grpcurl /usr/local/bin/grpcurl /usr/local/bin/grpcurl

# "cpu" or "gpu"; the Makefile passes it to match the base image's generation.
# A gpu worker advertises the flavor and turns on nvproxy (SANDBOX_GPU defaults
# from it). It must match the checkpoints baked into BASE_IMAGE.
ARG WORKER_FLAVOR=cpu

ENV SERVICE_NAME=worker
ENV PORT=50052
ENV WORKER_DIR=/shared
ENV ROUTER_URI=router:50051
ENV RUNSC_BINARY=/usr/local/bin/runsc
ENV RUNSC_ROOT=/run/readie-runsc
ENV WORKER_FLAVOR=${WORKER_FLAVOR}
ENV APP_ENV=production
ENV LOG_LEVEL=info
ENV LOG_FORMAT=json

EXPOSE 50052

CMD ["/app/go-server"]
