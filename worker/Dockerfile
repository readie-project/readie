FROM golang:1.25-bookworm AS go-builder

WORKDIR /app

# Dependencies are copied first so the module cache layer survives source edits.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o go-server ./cmd/worker

# grpcurl backs the compose healthcheck, so unlike before it must actually
# reach the runtime stage. The archive is architecture-specific; derive it
# rather than pinning x86_64, which broke arm64 builders.
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

# NOTE: gVisor is not installed here. The previous Dockerfile installed runsc
# in the builder stage — behind `sudo`, which golang:1.24-bookworm does not
# ship, so those layers failed — and never copied it into the runtime image.
# Running executors under gVisor needs runsc registered as a runtime on the
# Docker *daemon's* host, not inside this container; see the project README.
FROM gcr.io/kaggle-images/python
WORKDIR /app

COPY --from=go-builder /app/go-server .
COPY --from=tools /usr/local/bin/grpcurl /usr/local/bin/grpcurl

# The worker bind-mounts this tree into every executor container, so the path
# is resolved at build time and read back at startup. The value is trimmed on
# load: print appends a newline that would otherwise corrupt the bind spec.
RUN python -c "import site; print(site.getsitepackages()[0])" > /app/sitepackages_path.txt

# Per-container working directories live here. Executor containers are siblings
# of this one, and the daemon resolves bind sources on the host, so this path
# must resolve identically on both sides — see docker-compose.yml.
RUN mkdir -p /shared

ENV SERVICE_NAME=worker
ENV PORT=50052
ENV WORKER_DIR=/shared
ENV ROUTER_URI=router:50051
ENV SITEPACKAGES_TXT_PATH=/app/sitepackages_path.txt
ENV APP_ENV=production
ENV LOG_LEVEL=info
ENV LOG_FORMAT=json

EXPOSE 50052

CMD ["./go-server"]
