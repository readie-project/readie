# The offline checkpoint pipeline.
#
# It analyses the request corpus, decides which package sets are worth
# pre-importing, and captures one gVisor checkpoint per set against the shared
# executor rootfs. The rootfs and the checkpoints together form one generation,
# which the worker loads as a unit — a checkpoint only restores into the
# filesystem it was captured from.

# The OCI spec generator, built from the worker's own spec builder. Using the
# worker's code here is what keeps the sandbox this pipeline captures identical
# to the one the worker restores into; two independent generators would drift
# and every restore would fail opaquely.
FROM golang:1.25-bookworm AS ocispec-builder
WORKDIR /src
COPY worker/go.mod worker/go.sum ./
RUN go mod download
COPY worker/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /ocispec ./cmd/ocispec

# The executor root filesystem. Defined once in rootfs/Dockerfile and consumed
# by both this pipeline and the worker.
FROM crfs-executor-rootfs:latest AS rootfs_image

FROM python:3.12-slim

# gVisor, pinned by release: the checkpoint format is not stable across
# versions, so a floating runsc would silently invalidate every checkpoint the
# next time this image is rebuilt. The worker pins the same release.
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
    && chmod 0755 runsc && mv runsc /usr/local/bin/runsc \
    && rm -f runsc.sha512 \
    && rm -rf /var/lib/apt/lists/*
RUN runsc --version

WORKDIR /app

COPY scripts/ .

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r dataset/requirements.txt

RUN mkdir -p executorfs/rootfs executor/checkpoints

COPY --from=rootfs_image / ./executorfs/rootfs
COPY --from=ocispec-builder /ocispec /usr/local/bin/ocispec

# BASE_DIR is the OCI bundle: config.json plus the rootfs.
# EXECUTOR_DIR is a host-side output directory. It is deliberately distinct
# from the sandbox's own EXECUTOR_DIR (/tmp, where the executor binds its
# socket) — conflating the two previously sent the pre-imported executor source
# to the wrong place, so every checkpoint captured a bare executor.
ENV BASE_DIR="/app/executorfs"
ENV EXECUTOR_DIR="/app/executor"
ENV BUILD_CHECKPOINTS_NAME="checkpoints.json"
ENV CHECKPOINTS_DIR_NAME="checkpoints"
ENV EXECUTOR_NAME="app.py"

# Must correspond to the rootfs image above.
ENV ROOTFS_PYTHONPATH="/lib/python3.12/dist-packages"

# Runtime modes. These are part of what a checkpoint is sensitive to, so they
# must match the worker's configuration.
ENV SANDBOX_NETWORK="none"
ENV SANDBOX_HOST_UDS="create"
ENV SANDBOX_OVERLAY="root:memory"

RUN python -u setup.py

ENTRYPOINT ["python", "-u", "main.py"]
