import json
import os
import time

from checkpoints import build_checkpoint
from spec import runsc_version

# Global runsc flags. They precede every subcommand, and they are part of what
# a checkpoint is sensitive to, so they must match what the worker uses.
GLOBAL_FLAGS = [
    f"--network={os.environ.get('SANDBOX_NETWORK', 'none')}",
    f"--host-uds={os.environ.get('SANDBOX_HOST_UDS', 'create')}",
    f"--overlay2={os.environ.get('SANDBOX_OVERLAY', 'root:memory')}",
    "--ignore-cgroups",
]

executor_fs_dir = os.environ["BASE_DIR"]
executor_dir = os.environ["EXECUTOR_DIR"]
executor_name = os.environ["EXECUTOR_NAME"]
build_checkpoints_path = os.path.join(executor_dir, os.environ["BUILD_CHECKPOINTS_NAME"])

generation_id = os.environ.get("GENERATION_ID") or time.strftime(
    "gen-%Y%m%d-%H%M%S", time.gmtime()
)
rootfs_id = os.environ.get("ROOTFS_ID", "")

print(f"[*] Loading generated checkpoints...")
with open(build_checkpoints_path, 'r') as f:
    checkpoints = json.load(f)

with open(os.path.join(executor_dir, "spec-fingerprint.txt")) as f:
    spec_fingerprint = f.read().strip()

version = runsc_version()
print(f"[*] Building checkpoints with {version}")
print(f"[*] Generation {generation_id}")

# runsc takes the bundle from the working directory.
os.chdir(executor_fs_dir)

checkpoints_dir = os.path.join(executor_dir, os.environ["CHECKPOINTS_DIR_NAME"])

# The executor the sandbox actually runs lives inside the rootfs. Building this
# path with os.path.join(base, "/app/executor", name) silently discarded `base`
# — an absolute component resets the join — so the pre-imports were written to
# the host copy and every checkpoint captured a bare executor with nothing
# loaded, which is precisely the thing checkpoints exist to avoid.
rootfs_executor_path = os.path.join(
    executor_fs_dir, "rootfs", "app", "executor", executor_name
)
with open(rootfs_executor_path, 'r') as f:
    executor_code = f.read()

try:
    for i, checkpoint in enumerate(checkpoints):
        print(f"[*] Checkpoint {i + 1}:")
        checkpoint_path = build_checkpoint(
            f"checkpoint_{i + 1}",
            executor_code,
            rootfs_executor_path,
            checkpoints_dir,
            checkpoint,
            global_flags=GLOBAL_FLAGS,
            runsc_version=version,
            spec_fingerprint=spec_fingerprint,
            rootfs_id=rootfs_id,
            generation_id=generation_id,
        )
        print(f"[*] Checkpoint {i + 1} created at {checkpoint_path}")
finally:
    # Revert back to original executor code
    with open(rootfs_executor_path, "w") as f:
        f.write(executor_code)

# The generation manifest is what lets the worker pair these checkpoints with
# the filesystem they were captured against. A checkpoint that travels without
# it cannot be verified before a restore is attempted.
generation = {
    "id": generation_id,
    "rootfs_id": rootfs_id,
    "runsc_version": version,
    "spec_fingerprint": spec_fingerprint,
    "executor_entrypoint": os.path.join("/app/executor", executor_name),
    "python_path": os.environ["ROOTFS_PYTHONPATH"],
    "overlay": os.environ.get("SANDBOX_OVERLAY", "root:memory"),
    "network": os.environ.get("SANDBOX_NETWORK", "none"),
    "root_readonly": False,
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
with open(os.path.join(executor_dir, "generation.json"), "w") as f:
    json.dump(generation, f, indent=2)
    f.write("\n")

print(f"[*] Successfully built all checkpoints for generation {generation_id}")
print(f"[*] Export {executor_dir}/generation.json, {checkpoints_dir}/ and the rootfs")
print(f"[*] as <ARTIFACT_ROOT>/generations/{generation_id}/ on the worker.")
