# Checkpoint pipeline

Analyses the request corpus, decides which package sets are worth pre-importing,
and captures one gVisor checkpoint per set. The output is a **generation**: the
executor root filesystem plus every checkpoint taken against it.

The two ship together because a gVisor checkpoint only restores into the
filesystem it was captured from. A checkpoint that travels without its rootfs
is unusable.

## Build

The rootfs is defined once, in `../rootfs/Dockerfile`, and consumed by both this
pipeline and the worker. Build it first:

```sh
cd ..
docker build -t crfs-executor-rootfs:latest -f rootfs/Dockerfile .
docker build -t checkpoint-create -f scripts/Dockerfile .
```

## Run

gVisor needs to create namespaces and install seccomp filters, which the default
profiles block:

```sh
mkdir -p out
docker run --rm \
  --privileged \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  -e GENERATION_ID=gen-$(date -u +%Y%m%d-%H%M%S) \
  -e ROOTFS_ID=$(docker inspect --format '{{index .RepoDigests 0}}' crfs-executor-rootfs:latest) \
  -v "$PWD/out:/app/executor" \
  checkpoint-create
```

`ROOTFS_ID` is the identity the worker checks a checkpoint against, so it should
be the image digest rather than a tag.

## Install a generation on a worker

```sh
GEN=<generation id from the run output>
DEST=/var/lib/crfs/generations/$GEN

mkdir -p "$DEST/rootfs"
cp out/generation.json "$DEST/"
cp -r out/checkpoints  "$DEST/"

docker create --name rootfs-export crfs-executor-rootfs:latest
docker export rootfs-export | tar -x -C "$DEST/rootfs"
docker rm rootfs-export
```

The worker scans `$ARTIFACT_ROOT/generations/` at startup. It can hold several
generations at once, so installing a new one alongside the old lets warm
sandboxes finish on the filesystem they started with.

## How a checkpoint is captured

`setup.py` analyses the corpus, writes `checkpoints.json`, and generates the
bundle's `config.json` by calling `ocispec` — the worker's own spec builder.
That indirection is the point: the sandbox this pipeline captures and the one
the worker restores into must have the same shape, and two independent
generators cannot be kept in agreement by review.

`main.py` then, for each checkpoint, prepends the chosen `import` statements to
the executor **inside the rootfs**, runs the sandbox, waits for the executor to
print `READY_FOR_CHECKPOINT`, and captures the process mid-sleep.

The 30-second sleep after that sentinel is the whole capture window. It also
means a restored sandbox resumes mid-sleep and finishes the remainder before
binding its socket, which is why the worker's dial budget exceeds 30 seconds.

## Layout

| Path | |
|---|---|
| `$BASE_DIR` (`/app/executorfs`) | the OCI bundle: `config.json` + `rootfs/` |
| `$EXECUTOR_DIR` (`/app/executor`) | host-side outputs; mount this out |
| `$EXECUTOR_DIR/checkpoints/<id>/` | one checkpoint image plus its `meta.json` |
| `$EXECUTOR_DIR/generation.json` | the manifest binding those checkpoints to a rootfs |

`$EXECUTOR_DIR` here is a *host* directory and is deliberately distinct from the
sandbox's own `EXECUTOR_DIR`, which is `/tmp` — where the executor binds its
socket. Conflating the two previously wrote the pre-imported executor to the
wrong path, so every checkpoint captured a bare executor with nothing loaded.

## Known gaps

- `generate_checkpoints` is a stub returning one fixed set (`pandas`, `numpy`). The selection model it is meant to implement is not written.
- `datasets`, `tokenizers` and `models` are carried through the schema but unused.
- The rootfs is the full base image regardless of what the analysis selects. That is deliberate — a new request distribution then regenerates only checkpoints, not tens of gigabytes of rootfs — but it does mean the sandbox carries far more than any one checkpoint needs.
