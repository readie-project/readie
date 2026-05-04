import os
import subprocess
import shutil
import json

from dataset import get_resources, get_dataset
from metadata import generate_metadata
from checkpoints import generate_checkpoints, build_checkpoint, checkpoints_dir

packages, datasets, models, tokenizers = get_resources()

print(f"[*] Generating metadata...")
metadata = generate_metadata(packages, datasets, models, tokenizers)

dataset = get_dataset()

print(f"[*] Generating optimal checkpoints...")
checkpoints = generate_checkpoints(dataset, metadata)
print(f"[*] Optimal checkpoints generated: {len(checkpoints)}")

executor_path = "/app/executor/app.py"

print(f"[*] Preparing root filesystem...")
if os.path.exists(checkpoints_dir):
    shutil.rmtree(checkpoints_dir)
os.makedirs(checkpoints_dir, exist_ok=True)
rootfs_dir = os.path.join(checkpoints_dir, "rootfs")
os.makedirs(rootfs_dir, exist_ok=True)
image_name = os.environ.get("ROOTFS_IMAGE_NAME")
subprocess.run(f"docker export $(docker create {image_name}) | tar -C {rootfs_dir} -xvf -",
               shell=True, check=True, capture_output=True, text=True)
print(f"[*] Root filesystem prepared at {rootfs_dir}")

print(f"[*] Preparing runsc config...")
os.chdir(checkpoints_dir)
subprocess.run(f"runsc spec", shell=True, check=True,
               capture_output=True, text=True)
config_path = os.path.join(checkpoints_dir, "config.json")
with open(config_path, "r") as f:
    config = json.load(f)

# Change run command to the executor file
binary_path = os.environ.get("ROOTFS_BINARY_PATH")
config["args"] = ["python", "-u", executor_path]
env = config["env"]

# Update the PATH to include python and other binaries
path_str = "PATH="
for i in range(len(env)):
    if env[i].startswith(path_str):
        env[i] = env[i][:len(path_str)] + binary_path + \
            ":" + env[i][len(path_str):]
        break

# Update PYTHONPATH
env.append(f"PYTHONPATH={os.environ.get("ROOTFS_PYTHONPATH")}")
config["env"] = env

with open(config_path, "w") as f:
    json.dump(config, f, indent=4)
print(f"[*] Runsc config updated at {config_path}")

print(f"[*] Building checkpoints...")
with open(executor_path, 'r') as f:
    executor_code = f.read()
try:
    for i, checkpoint in enumerate(checkpoints):
        print(f"[*] Checkpoint {i}:")
        build_checkpoint(
            f"checkpoint_{i}", executor_code, executor_path, checkpoint)
finally:
    # Revert back to original executor code
    with open(executor_path, "w") as f:
        f.write(executor_code)

print(f"[*] Successfully built all checkpoints")
