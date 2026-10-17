# The offline pipeline, and the base image it produces for the worker.
#
# A gVisor checkpoint only restores into the filesystem it was captured from and
# through the runsc release that captured it. Both of those, and the checkpoints
# themselves, are defined here - one file, so the tree this captures against and
# the tree the worker restores into cannot drift, and so the gVisor release is
# pinned exactly once.
#
# Three targets:
#
#   --target analyzer      `readie-pipeline analyze` alone, run natively against
#                          the base image with no gVisor and no rootfs copy.
#   --target pipeline      the capture tool. Run privileged, because runsc needs
#                          namespace access `docker build` cannot grant.
#   --target worker-base   what a worker runs on: runsc, the root filesystem, and
#                          the captured checkpoints. ~26 GB and slow.
#
# The worker's own image is *not* here - see worker/Dockerfile, which is `FROM`
# the base and adds only the Go server and grpcurl. Everything version-sensitive
# is in the base; everything that changes when worker code changes is in that
# file. So a worker rebuild never touches 26 GB.
#
# The build context is the repository root: this needs executor/, worker/ (for
# ocispec) and pipeline/, and the first two do not live under pipeline/.

# The rootfs base image publishes no architecture but amd64, and gVisor works by
# intercepting syscalls - exactly what emulation sits in the middle of. Declared
# as an ARG rather than written into the FROM: BuildKit's linter rejects a
# constant --platform, and this way an experiment can override it.
ARG ROOTFS_PLATFORM=linux/amd64
# The rootfs base image. The Makefile selects it per FLAVOR.
ARG ROOTFS_BASE_IMAGE=python:3.12-slim

# ---------------------------------------------------------------------------
# The executor, built as a wheel rather than copied as a loose file. A wheel
# carries its own metadata and entry point, so the sandbox runs
# `python -m readie_executor` and the rootfs has no idea where the source lived.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS executor-wheel
RUN pip install --no-cache-dir build
WORKDIR /src
COPY executor/ ./
RUN python -m build --wheel --outdir /dist

# ---------------------------------------------------------------------------
# The executor root filesystem: the one tree every sandbox runs against and
# every checkpoint is captured from. Consumed twice below - by the pipeline as
# an OCI bundle root, and by the worker base at /var/lib/readie/rootfs.
# ---------------------------------------------------------------------------
FROM --platform=${ROOTFS_PLATFORM} ${ROOTFS_BASE_IMAGE} AS rootfs

# The base image ships an empty /etc/resolv.conf, and nothing populates it at
# container-create time the way a normal container runtime would - a gVisor
# sandbox runs directly against this rootfs with no host bind-mount. Without a
# nameserver, every DNS lookup inside the sandbox fails.
#
# This has to be baked at a path other than /etc/resolv.conf itself: Docker
# treats that exact path (along with /etc/hosts and /etc/hostname) as a
# runtime-managed file and silently drops anything written to it here, whether
# by RUN (fails outright, "Device or resource busy") or COPY (succeeds but the
# content never survives into the exported layer). readie_executor copies this
# into place at /etc/resolv.conf itself before it needs DNS, since only it -
# not this build - ever runs against the real, unmanaged rootfs.
RUN printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > /etc/readie-resolv.conf

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY executor/pyproject.toml executor/uv.lock /tmp/build/
WORKDIR /tmp/build

# Export the locked environment (excluding the project itself) as a
# requirements file and install it into system Python.
# --frozen guarantees build fails if uv.lock doesn't match pyproject.toml
# --no-emit-project ensures we only get third-party dependencies, not readie-executor.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt > requirements.txt \
    && uv pip install --system --no-cache-dir -r requirements.txt

# Install the pre-built executor wheel without touching dependencies again
# --no-deps: resolving dependencies here would let pip upgrade a package the
# checkpoints were captured with. The rootfs must be reproducible from the base image alone.
WORKDIR /
RUN --mount=from=executor-wheel,source=/dist,target=/dist \
    uv pip install --system --no-cache-dir --no-deps /dist/*.whl

# System libraries that installed wheels expect from the base image, as a comma
# separated list of Debian or Ubuntu packages. A pip install can succeed and the
# import still fail on a missing shared object, and that failure only appears when
# a checkpoint is captured. A base that has no apt-get fails here, with a message,
# when the list is not empty.
ARG ROOTFS_APT_PACKAGES=
RUN if [ -n "${ROOTFS_APT_PACKAGES}" ]; then \
        command -v apt-get >/dev/null \
            || { echo "ROOTFS_APT_PACKAGES needs a Debian or Ubuntu base image" >&2; exit 1; }; \
        apt-get update \
        && apt-get install -y --no-install-recommends $(echo "${ROOTFS_APT_PACKAGES}" | tr ',' ' ') \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# The distributions the checkpoints' packages need, on top of the base image.
# `readie-pipeline plan` writes the list (see planning/requirements.py), so a base
# that carries no packages of its own (alpine, python-slim) ends up with exactly
# what the plan imports. Each line is `name==version`, the version the metadata
# recorded from the Kaggle image, so the timings and sizes the plan was built from
# describe what gets installed; a distribution with no recorded version is a bare
# name. The default file is empty, which installs nothing and leaves a full base
# image such as Kaggle's as it is.
#
# Each line is installed on its own, so one distribution that will not install (no
# wheel for this libc or architecture, or a conflict) is reported instead of failing
# the rest. Every install gets two sets of constraints: the executor's locked
# versions, so the executor keeps the dependencies it was built with, and every
# pin in the list, so a later line's dependencies cannot move an earlier pin.
# Without the second, installing line by line let a later package pull a newer
# numpy, numba or grpcio over the pinned one. Whatever fails is
# listed in /etc/readie-rootfs-skipped.txt; `capture` inventories the rootfs
# afterwards, so the catalogue lists only what really installed. Set
# ROOTFS_REQUIREMENTS_STRICT=true to fail the build instead.
# Extra package indexes, for pins that name a build the default index lacks.
# Comma separated when there are several: a space would split the build argument in
# the Makefile, so the commas become the spaces that uv's list takes. With any set,
# uv looks at every index for the best match, so a package that exists on both is not
# taken from an extra one by default; leave it empty for a base that needs none.
ARG ROOTFS_EXTRA_INDEX_URL=
ARG ROOTFS_REQUIREMENTS=pipeline/rootfs-requirements.txt
ARG ROOTFS_REQUIREMENTS_STRICT=false
COPY ${ROOTFS_REQUIREMENTS} /tmp/build/rootfs-requirements.txt
RUN set -eu; \
    : > /etc/readie-rootfs-skipped.txt; \
    grep -v '^#' /tmp/build/rootfs-requirements.txt | grep '==' > /tmp/build/rootfs-pins.txt || true; \
    if [ -n "${ROOTFS_EXTRA_INDEX_URL}" ]; then \
        export UV_EXTRA_INDEX_URL="$(echo "${ROOTFS_EXTRA_INDEX_URL}" | tr ',' ' ')" \
               UV_INDEX_STRATEGY=unsafe-best-match; \
    fi; \
    while IFS= read -r dist || [ -n "$dist" ]; do \
        case "$dist" in ''|'#'*) continue ;; esac; \
        uv pip install --system --no-cache-dir \
            -c /tmp/build/requirements.txt -c /tmp/build/rootfs-pins.txt "$dist" \
            || echo "$dist" >> /etc/readie-rootfs-skipped.txt; \
    done < /tmp/build/rootfs-requirements.txt; \
    if [ -s /etc/readie-rootfs-skipped.txt ]; then \
        echo "==> not installed into the rootfs:"; cat /etc/readie-rootfs-skipped.txt; \
        [ "${ROOTFS_REQUIREMENTS_STRICT}" != "true" ] || exit 1; \
    fi

# Clean up temporary build files. /bin/uv is kept: the worker runs it, through
# `runsc exec`, to install a call's requested packages before the function runs.
RUN rm -rf /tmp/build

# Fail the build rather than the first restore if the entry point is not
# importable. A rootfs whose executor cannot start produces checkpoints that
# all fail identically, minutes into a request.
RUN python -c "import readie_executor; print('readie-executor', readie_executor.__version__)"

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
# checkpoint - which is why this stage exists rather than an install in each.
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
# Common host layer: the runtime and the guest root filesystem.
# Both the pipeline and the worker-base inherit from this stage so that the
# massive rootfs is copied exactly once, eliminating duplicate ~26 GB storage.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS host-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=runsc /usr/local/bin/runsc /usr/local/bin/runsc

# Fail the build rather than deployment if the runtime is unusable.
RUN runsc --version

# The one root filesystem every sandbox runs against.
COPY --from=rootfs / /var/lib/readie/rootfs

# ---------------------------------------------------------------------------
# The analyzer: `readie-pipeline analyze`, run natively inside a container
# built directly from the base image, so it measures exactly what that image
# ships with no cross-environment tricks needed.
#
# This used to run inside `pipeline` below via PYTHONPATH into `host-base`'s
# copied rootfs -- but that mixes this stage's own libc with the base image's
# copy of *its* libc, glibc and friends included, the moment anything beyond
# pure Python is involved. Confirmed against a real build: with that PYTHONPATH
# set, even `import pandas` crashed with SIGFPE, not just the packages it was
# meant to additionally reach (cv2 and the like, which need OS-level shared
# libraries no PYTHONPATH addition can supply safely). Building from the base
# image directly instead means there is only one libc, because there is only
# one image -- nothing to mismatch.
# ---------------------------------------------------------------------------
FROM --platform=${ROOTFS_PLATFORM} ${ROOTFS_BASE_IMAGE} AS analyzer

WORKDIR /app

COPY pipeline/pyproject.toml pipeline/README.md ./
COPY pipeline/src ./src
COPY pipeline/data ./data

# --no-deps: numpy and packaging are already whatever this base image ships
# (analyze doesn't need a newer one; it never touches the planner's own numpy
# use). Resolving readie-pipeline's own requirement here could upgrade the
# base image's copy before measuring it -- exactly the kind of interference
# `rootfs` above avoids for the executor with the same flag.
RUN pip install --no-cache-dir --no-deps .

ENV READIE_DATA_DIR="/app/data"

ENTRYPOINT ["readie-pipeline"]
CMD ["analyze"]

# ---------------------------------------------------------------------------
# The capture tool. It analyses the request corpus, decides which package sets
# are worth pre-importing, and captures one gVisor checkpoint per set against
# the rootfs above.
# ---------------------------------------------------------------------------
FROM host-base AS pipeline

WORKDIR /app

# Installed as a package, so the stages are `readie-pipeline plan` and
# `readie-pipeline build` rather than two scripts that execute on import.
# Without the `corpus` extra: generating snippets calls a hosted model and is an
# authoring step, not part of building checkpoints.
#
# Copied file by file rather than as `pipeline/`: that directory also holds out/,
# which is where a capture writes multi-gigabyte checkpoint images, and a whole-
# directory COPY would bake the previous generation's output into this image. A
# .dockerignore cannot solve it - the worker-base target below has to COPY out/.
COPY pipeline/pyproject.toml pipeline/README.md ./
COPY pipeline/src ./src
COPY pipeline/data ./data
RUN pip install --no-cache-dir .

RUN mkdir -p executor/checkpoints plan

COPY --from=ocispec-builder /ocispec /usr/local/bin/ocispec

# BASE_DIR is the OCI bundle: config.json plus the rootfs.
# EXECUTOR_DIR is a host-side output directory. It is deliberately distinct
# from the sandbox's own EXECUTOR_DIR (/tmp, where the executor binds its
# socket) - conflating the two previously sent the pre-imported executor source
# to the wrong place, so every checkpoint captured a bare executor.
ENV BASE_DIR="/var/lib/readie"
ENV EXECUTOR_DIR="/app/executor"
ENV READIE_DATA_DIR="/app/data"

# The plan and the spec fingerprint are inputs to a capture, not artifacts a
# worker ships. EXECUTOR_DIR is mounted out and then copied into the base image
# wholesale, so keeping these apart is what stops them being baked into
# /var/lib/readie beside the manifest.
ENV READIE_PLAN_DIR="/app/plan"

# Must correspond to the rootfs stage above: Kaggle's Python keeps packages in
# dist-packages, the python:3.12 images (slim, alpine) in site-packages.
ARG ROOTFS_PYTHONPATH=/usr/local/lib/python3.12/dist-packages
ENV ROOTFS_PYTHONPATH=${ROOTFS_PYTHONPATH}

# Runtime modes. These are part of what a checkpoint is sensitive to, so they
# must match the worker's configuration.
ENV SANDBOX_NETWORK="sandbox"
ENV SANDBOX_HOST_UDS="create"
ENV SANDBOX_OVERLAY="root:memory"

# How long a sandbox may take checkpoint before capture fails.
ENV READIE_CHECKPOINT_TIMEOUT="300.0"
ENV OCISPEC_BINARY="/usr/local/bin/ocispec"
ENV RUNSC_BINARY="runsc"

# `capture` plans and then captures in one process. Planning cannot run at build
# time: it writes the bundle's config.json under BASE_DIR, and a `docker run`
# that mounts EXECUTOR_DIR out is a different container with a different
# BASE_DIR - so the two stages have to share one.
ENTRYPOINT ["readie-pipeline"]
CMD ["capture"]

# ---------------------------------------------------------------------------
# The worker's base layer: the pinned runsc and the root filesystem the
# checkpoints were captured against. The checkpoints themselves are not in this
# image: `make capture` writes them (and manifest.json) to pipeline/out/<flavor>,
# and docker-compose.yml mounts them into the worker at /var/lib/readie. They stay
# paired with this image because `make generation` builds both from the same
# `rootfs` stage.
#
# worker/Dockerfile builds `FROM` this image and adds only what is independent of
# it: the Go server and grpcurl. That split is why a worker code change rebuilds in
# seconds without touching 26 GB, and why the rootfs layer is *inherited* rather
# than copied - a layer inherited from a base image has the same digest by
# definition, whereas `COPY --from=` emits a fresh one every build (two
# otherwise-identical builds were observed differing in the 25.9 GB rootfs layer's
# digest).
#
# Re-capturing does not rebuild this image at all. It does not depend on the
# capture, so it can be built before, after or without one.
#
#   make worker-base
# ---------------------------------------------------------------------------
FROM host-base AS worker-base

# /shared holds per-sandbox directories and OCI bundles, and is the one thing
# still a volume: it is writable runtime scratch. /run/readie-runsc is the
# runtime's state directory, which the worker owns exclusively.
RUN mkdir -p /shared /run/readie-runsc /var/log/runsc

# manifest.json and checkpoints/ arrive at /var/lib/readie as bind mounts, not
# layers. Without them the worker still starts and serves cold starts only.
