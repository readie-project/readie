# The offline pipeline, and the base image it produces for the worker.
#
# A gVisor checkpoint only restores into the filesystem it was captured from and
# through the runsc release that captured it. Both of those, and the checkpoints
# themselves, are defined here — one file, so the tree this captures against and
# the tree the worker restores into cannot drift, and so the gVisor release is
# pinned exactly once.
#
# Two targets:
#
#   --target pipeline      the capture tool. Run privileged, because runsc needs
#                          namespace access `docker build` cannot grant.
#   --target worker-base   what a worker runs on: runsc, the root filesystem, and
#                          the captured checkpoints. ~26 GB and slow.
#
# The worker's own image is *not* here — see worker/Dockerfile, which is `FROM`
# the base and adds only the Go server and grpcurl. Everything version-sensitive
# is in the base; everything that changes when worker code changes is in that
# file. So a worker rebuild never touches 26 GB.
#
# The build context is the repository root: this needs executor/, worker/ (for
# ocispec) and pipeline/, and the first two do not live under pipeline/.

# The rootfs base image publishes no architecture but amd64, and gVisor works by
# intercepting syscalls — exactly what emulation sits in the middle of. Declared
# as an ARG rather than written into the FROM: BuildKit's linter rejects a
# constant --platform, and this way an experiment can override it.
ARG ROOTFS_PLATFORM=linux/amd64
# The rootfs base image. A cpu generation uses the plain Kaggle image; a gpu
# generation passes gcr.io/kaggle-gpu-images/python (CUDA + the GPU stack). The
# Makefile selects it per FLAVOR.
ARG ROOTFS_BASE_IMAGE=gcr.io/kaggle-images/python

# ---------------------------------------------------------------------------
# The executor, built as a wheel rather than copied as a loose file. A wheel
# carries its own metadata and entry point, so the sandbox runs
# `python -m crfs_executor` and the rootfs has no idea where the source lived.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS executor-wheel
RUN pip install --no-cache-dir build
WORKDIR /src
COPY executor/ ./
RUN python -m build --wheel --outdir /dist

# ---------------------------------------------------------------------------
# The executor root filesystem: the one tree every sandbox runs against and
# every checkpoint is captured from. Consumed twice below — by the pipeline as
# an OCI bundle root, and by the worker base at /var/lib/crfs/rootfs.
# ---------------------------------------------------------------------------
FROM --platform=${ROOTFS_PLATFORM} ${ROOTFS_BASE_IMAGE} AS rootfs

# --no-deps: cloudpickle is already in the base image, and resolving
# dependencies here would let pip upgrade a package the checkpoints were
# captured with. The rootfs must be reproducible from the base image alone.
RUN --mount=from=executor-wheel,source=/dist,target=/dist \
    pip install --no-cache-dir --no-deps /dist/*.whl

# Fail the build rather than the first restore if the entry point is not
# importable. A rootfs whose executor cannot start produces checkpoints that
# all fail identically, minutes into a request.
RUN python -c "import crfs_executor; print('crfs-executor', crfs_executor.__version__)"

# ---------------------------------------------------------------------------
# The OCI spec generator, built from the worker's own spec builder. Using the
# worker's code here is what keeps the sandbox this pipeline captures identical to
# the one the worker restores into; two independent generators would drift and
# every restore would fail opaquely.
#
# Only ocispec is built. The worker's server is built by worker/Dockerfile, which
# owns its own image.
# ---------------------------------------------------------------------------
FROM golang:1.25-bookworm AS ocispec-builder
WORKDIR /src

# Dependencies first, so the module cache layer survives source edits.
COPY worker/go.mod worker/go.sum ./
RUN go mod download

COPY worker/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /ocispec ./cmd/ocispec

# ---------------------------------------------------------------------------
# gVisor, pinned by release, installed once for both the pipeline that captures
# checkpoints and the worker that restores them. The checkpoint format is not
# stable across releases, so those two versions disagreeing invalidates every
# checkpoint — which is why this stage exists rather than an install in each.
#
# The apt package is deliberately avoided: its postinst edits
# /etc/docker/daemon.json, which is meaningless inside an image, and the
# repository only ever serves the latest build.
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# The capture tool. It analyses the request corpus, decides which package sets
# are worth pre-importing, and captures one gVisor checkpoint per set against
# the rootfs above.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS pipeline

COPY --from=runsc /usr/local/bin/runsc /usr/local/bin/runsc

# Fail the build rather than the first capture if the runtime is unusable.
RUN runsc --version

WORKDIR /app

# Installed as a package, so the stages are `crfs-pipeline plan` and
# `crfs-pipeline build` rather than two scripts that execute on import.
# Without the `corpus` extra: generating snippets calls a hosted model and is an
# authoring step, not part of building checkpoints.
#
# Copied file by file rather than as `pipeline/`: that directory also holds out/,
# which is where a capture writes multi-gigabyte checkpoint images, and a whole-
# directory COPY would bake the previous generation's output into this image. A
# .dockerignore cannot solve it — the worker-base target below has to COPY out/.
COPY pipeline/pyproject.toml pipeline/README.md ./
COPY pipeline/src ./src
COPY pipeline/data ./data
RUN pip install --no-cache-dir .

RUN mkdir -p executorfs/rootfs executor/checkpoints plan

COPY --from=rootfs / ./executorfs/rootfs
COPY --from=ocispec-builder /ocispec /usr/local/bin/ocispec

# BASE_DIR is the OCI bundle: config.json plus the rootfs.
# EXECUTOR_DIR is a host-side output directory. It is deliberately distinct
# from the sandbox's own EXECUTOR_DIR (/tmp, where the executor binds its
# socket) — conflating the two previously sent the pre-imported executor source
# to the wrong place, so every checkpoint captured a bare executor.
ENV BASE_DIR="/app/executorfs"
ENV EXECUTOR_DIR="/app/executor"
ENV CRFS_DATA_DIR="/app/data"

# The plan and the spec fingerprint are inputs to a capture, not artifacts a
# worker ships. EXECUTOR_DIR is mounted out and then copied into the base image
# wholesale, so keeping these apart is what stops them being baked into
# /var/lib/crfs beside the manifest.
ENV CRFS_PLAN_DIR="/app/plan"

# Must correspond to the rootfs stage above.
ENV ROOTFS_PYTHONPATH="/lib/python3.12/dist-packages"

# Runtime modes. These are part of what a checkpoint is sensitive to, so they
# must match the worker's configuration.
ENV SANDBOX_NETWORK="none"
ENV SANDBOX_HOST_UDS="create"
ENV SANDBOX_OVERLAY="root:memory"

# How long a sandbox may take to print READY_FOR_CHECKPOINT before capture fails.
ENV CRFS_READY_TIMEOUT="300.0"
ENV OCISPEC_BINARY="/usr/local/bin/ocispec"
ENV RUNSC_BINARY="runsc"

# `capture` plans and then captures in one process. Planning cannot run at build
# time: it writes the bundle's config.json under BASE_DIR, and a `docker run`
# that mounts EXECUTOR_DIR out is a different container with a different
# BASE_DIR — so the two stages have to share one.
ENTRYPOINT ["crfs-pipeline"]
CMD ["capture"]

# ---------------------------------------------------------------------------
# The worker's base layer: everything a checkpoint's validity is bound to — the
# pinned runsc, the root filesystem, and the checkpoints captured against it.
#
# This is the whole of what the pipeline produces. worker/Dockerfile builds
# `FROM` this image and adds only what is independent of it: the Go server and
# grpcurl. That split is why a worker code change rebuilds in seconds without
# touching 26 GB, and why the rootfs layer is *inherited* rather than copied — a
# layer inherited from a base image has the same digest by definition, whereas
# `COPY --from=` emits a fresh one every build (two otherwise-identical builds
# were observed differing in the 25.9 GB rootfs layer's digest).
#
# Re-capturing rebuilds this image, but not that layer: the rootfs stage is
# unchanged, so its COPY is a cache hit and the new base differs from the old
# one only in the checkpoints on top.
#
#   make worker-base
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS worker-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=runsc /usr/local/bin/runsc /usr/local/bin/runsc

# Fail the build rather than the deployment if the runtime is unusable.
RUN runsc --version

# /shared holds per-sandbox directories and OCI bundles, and is the one thing
# still a volume: it is writable runtime scratch. /run/crfs-runsc is the
# runtime's state directory, which the worker owns exclusively.
RUN mkdir -p /shared /run/crfs-runsc /var/log/runsc

# The one root filesystem every sandbox runs against.
COPY --from=rootfs / /var/lib/crfs/rootfs

# manifest.json and checkpoints/, produced by `crfs-pipeline capture`.
#
# Empty by default — pipeline/out/ holds only a .gitkeep — so this still yields a
# working base: rootfs, no checkpoints, every start cold. `make generation`
# captures first, then builds this.
ARG ARTIFACTS_DIR=pipeline/out
COPY ${ARTIFACTS_DIR}/ /var/lib/crfs/
