# The executor root filesystem.
#
# Every sandbox runs against this tree, and every checkpoint is captured from
# it. A gVisor checkpoint only restores into the filesystem it was taken from,
# so this is defined once here and consumed by both the offline checkpoint
# pipeline (scripts/) and the worker — rather than assembled independently in
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
FROM gcr.io/kaggle-images/python

# The executor is the only thing added to the base. It is the program the
# sandbox runs, and its path is part of the generation manifest.
COPY scripts/executor/app.py /app/executor/app.py
