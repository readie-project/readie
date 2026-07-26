# The worker is a Go binary that drives runsc. It carries no Python and no
# executor root filesystem: the rootfs is a separate versioned artifact,
# mounted at $ARTIFACT_ROOT, so regenerating checkpoints does not mean
# rebuilding and redeploying tens of gigabytes of image.
FROM golang:1.25-bookworm AS go-builder

WORKDIR /app

# Dependencies first, so the module cache layer survives source edits.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o go-server ./cmd/worker
# ocispec generates OCI bundles from the same code the worker uses. The offline
# checkpoint pipeline runs it, which is what keeps the two in agreement.
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o ocispec ./cmd/ocispec

# grpcurl backs the compose healthcheck. The archive is architecture-specific,
# so derive it rather than pinning one.
FROM debian:bookworm-slim AS tools
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

# gVisor, pinned by release.
#
# The apt package is deliberately avoided: its postinst edits
# /etc/docker/daemon.json, which is meaningless inside an image, and the
# repository only ever serves the latest build. gVisor's checkpoint format is
# not stable across releases, so a floating runsc would silently invalidate
# every checkpoint on the next rebuild.
FROM debian:bookworm-slim AS runsc
ARG TARGETARCH
ARG GVISOR_RELEASE=20260721
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget \
    && case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
         amd64) GVISOR_ARCH=x86_64  ;; \
         arm64) GVISOR_ARCH=aarch64 ;; \
         *) echo "gVisor publishes no build for ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && URL="https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/${GVISOR_ARCH}" \
    && wget -q "${URL}/runsc" "${URL}/runsc.sha512" \
    && sha512sum -c runsc.sha512 \
    && chmod 0755 runsc \
    && mv runsc /usr/local/bin/runsc \
    && rm -f runsc.sha512 \
    && rm -rf /var/lib/apt/lists/*

FROM debian:bookworm-slim
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=go-builder /app/go-server /app/go-server
COPY --from=go-builder /app/ocispec   /usr/local/bin/ocispec
COPY --from=tools      /usr/local/bin/grpcurl /usr/local/bin/grpcurl
COPY --from=runsc      /usr/local/bin/runsc   /usr/local/bin/runsc

# Fail the build rather than the deployment if the runtime is unusable.
RUN runsc --version

# /shared holds per-sandbox directories and OCI bundles; /var/lib/crfs is where
# the rootfs-and-checkpoints artifact is mounted; /run/crfs-runsc is the
# runtime's state directory, which this worker owns exclusively.
RUN mkdir -p /shared /var/lib/crfs /run/crfs-runsc /var/log/runsc

ENV SERVICE_NAME=worker
ENV PORT=50052
ENV WORKER_DIR=/shared
ENV ARTIFACT_ROOT=/var/lib/crfs
ENV ROUTER_URI=router:50051
ENV RUNSC_BINARY=/usr/local/bin/runsc
ENV RUNSC_ROOT=/run/crfs-runsc
ENV APP_ENV=production
ENV LOG_LEVEL=info
ENV LOG_FORMAT=json

EXPOSE 50052

CMD ["/app/go-server"]
