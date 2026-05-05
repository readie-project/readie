import os
import json

from checkpoints import build_checkpoint

executor_fs_dir = os.environ.get("BASE_DIR")
executor_dir = os.environ.get("EXECUTOR_DIR")
executor_name = os.environ.get("EXECUTOR_NAME")
build_checkpoints_path = os.path.join(executor_dir, os.environ.get("BUILD_CHECKPOINTS_NAME"))

print(f"[*] Loading generated checkpoints...")
with open(build_checkpoints_path, 'r') as f:
    checkpoints = json.load(f)

print(f"[*] Building checkpoints...")
os.chdir(executor_fs_dir)
checkpoints_dir = os.path.join(executor_dir, os.environ.get("CHECKPOINTS_DIR_NAME"))
executor_path = os.path.join(executor_fs_dir, executor_dir, executor_name)
with open(executor_path, 'r') as f:
    executor_code = f.read()
try:
    for i, checkpoint in enumerate(checkpoints):
        print(f"[*] Checkpoint {i + 1}:")
        checkpoint_path = build_checkpoint(f"checkpoint_{i + 1}", executor_code, executor_path, checkpoints_dir, checkpoint)
        print(f"[*] Checkpoint {i + 1} created at {checkpoint_path}")
finally:
    # Revert back to original executor code
    with open(executor_path, "w") as f:
        f.write(executor_code)
print(f"[*] Successfully built all checkpoints")