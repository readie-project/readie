import os
import json

from dataset import get_resources, get_dataset
from metadata import generate_metadata
from checkpoints import generate_checkpoints
from spec import build_config

packages, datasets, models, tokenizers = get_resources()

print(f"[*] Generating metadata...")
metadata = generate_metadata(packages, datasets, models, tokenizers)

dataset = get_dataset()

# BASE_DIR is the OCI bundle: config.json plus the rootfs the sandbox runs.
# EXECUTOR_DIR is a *host* directory holding build outputs. It is deliberately
# distinct from the sandbox's own EXECUTOR_DIR (/tmp, where the executor binds
# its socket); conflating the two is what previously sent the pre-imported
# executor source to the wrong place.
executor_fs_dir = os.environ["BASE_DIR"]
executor_dir = os.environ["EXECUTOR_DIR"]
executor_name = os.environ["EXECUTOR_NAME"]

print(f"[*] Generating optimal checkpoints...")
checkpoints = generate_checkpoints(dataset, metadata)
build_checkpoints_path = os.path.join(executor_dir, os.environ["BUILD_CHECKPOINTS_NAME"])
with open(build_checkpoints_path, 'w') as f:
    json.dump(checkpoints, f, indent=4)
print(f"[*] Optimal checkpoints generated: {len(checkpoints)}, saved to {build_checkpoints_path}")

# The socket directory is bind-mounted into the sandbox. Nothing binds a socket
# during the build — the process is captured before it gets that far — but the
# mount must exist in the spec, because the mount list is part of what a
# checkpoint can be restored into.
socket_dir = os.path.join(executor_dir, "socket")
os.makedirs(socket_dir, exist_ok=True)

print(f"[*] Preparing runsc config...")
fingerprint = build_config(
    bundle_dir=executor_fs_dir,
    rootfs_path=os.path.join(executor_fs_dir, "rootfs"),
    # The path is inside the sandbox, so it resolves within the rootfs.
    executor_entrypoint=os.path.join("/app/executor", executor_name),
    python_path=os.environ["ROOTFS_PYTHONPATH"],
    socket_dir=socket_dir,
)
print(f"[*] Runsc config written to {os.path.join(executor_fs_dir, 'config.json')}")
print(f"[*] Spec fingerprint: {fingerprint}")

# Recorded so the build step can stamp it onto every checkpoint without
# regenerating the spec.
with open(os.path.join(executor_dir, "spec-fingerprint.txt"), "w") as f:
    f.write(fingerprint + "\n")
