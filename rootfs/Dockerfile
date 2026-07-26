# The executor root filesystem.
#
# Every sandbox runs against this tree, and every checkpoint is captured from
# it. A gVisor checkpoint only restores into the filesystem it was taken from,
# so this is defined once here and consumed by both the offline checkpoint
# pipeline (pipeline/) and the worker — rather than assembled independently in
# each, where the two could drift and break every restore.
#
# Build and export it as a versioned artifact:
#
#   docker build -t crfs-executor-rootfs:<tag> -f rootfs/Dockerfile .
#   docker create --name rootfs-export crfs-executor-rootfs:<tag>
#   mkdir -p /var/lib/crfs/generations/<gen>/rootfs
#   docker export rootfs-export | tar -x -C /var/lib/crfs/generations/<gen>/rootfs
#   docker rm rootfs-export
#
# The image digest is the rootfs identity recorded in each generation, which is
# what lets the worker refuse a checkpoint captured against a different tree.

# The executor, built as a wheel rather than copied as a loose file. A wheel
# carries its own metadata and entry point, so the sandbox runs
# `python -m crfs_executor` and the rootfs has no idea where the source lived.
FROM python:3.12-slim AS executor-builder
RUN pip install --no-cache-dir build
WORKDIR /src
COPY executor/ ./
RUN python -m build --wheel --outdir /dist

FROM gcr.io/kaggle-images/python

# --no-deps: cloudpickle is already in the base image, and resolving
# dependencies here would let pip upgrade a package the checkpoints were
# captured with. The rootfs must be reproducible from the base image alone.
RUN --mount=from=executor-builder,source=/dist,target=/dist \
    pip install --no-cache-dir --no-deps /dist/*.whl

# Fail the build rather than the first restore if the entry point is not
# importable. A rootfs whose executor cannot start produces checkpoints that
# all fail identically, minutes into a request.
RUN python -c "import crfs_executor; print('crfs-executor', crfs_executor.__version__)"
