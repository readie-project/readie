import os
import subprocess
import shutil
import json

from dataset import get_resources, get_dataset
from metadata import generate_metadata
from checkpoints import generate_checkpoints

packages, datasets, models, tokenizers = get_resources()

print(f"[*] Generating metadata...")
metadata = generate_metadata(packages, datasets, models, tokenizers)

dataset = get_dataset()

executor_fs_dir = os.environ.get("BASE_DIR")
executor_dir = os.environ.get("EXECUTOR_DIR")
executor_name = os.environ.get("EXECUTOR_NAME")

print(f"[*] Generating optimal checkpoints...")
checkpoints = generate_checkpoints(dataset, metadata)
build_checkpoints_path = os.path.join(executor_dir, os.environ.get("BUILD_CHECKPOINTS_NAME"))
with open(build_checkpoints_path, 'w') as f:
    json.dump(checkpoints, f, indent=4)
print(f"[*] Optimal checkpoints generated: {len(checkpoints)}, saved to {build_checkpoints_path}")

print(f"[*] Preparing runsc config...")
os.chdir(executor_fs_dir)
try:
    process = subprocess.run(f"runsc spec", shell=True, check=True,
                capture_output=True, text=True)
    print(process.stdout)
except subprocess.CalledProcessError as e:
    print(e.stderr)
    raise

config_path = os.path.join(executor_fs_dir, "config.json")
with open(config_path, "r") as f:
    config = json.load(f)

# Change run command to the executor file
binary_path = os.environ.get("ROOTFS_BINARY_PATH")
config["process"]["args"] = ["python", "-u", os.path.join(executor_dir, executor_name)]
env = config["process"]["env"]

# Update the PATH to include python and other binaries
path_str = "PATH="
for i in range(len(env)):
    if env[i].startswith(path_str):
        env[i] = env[i][:len(path_str)] + binary_path + \
            ":" + env[i][len(path_str):]
        break

# Update PYTHONPATH
env.append(f"PYTHONPATH={os.environ.get("ROOTFS_PYTHONPATH")}")
config["process"]["env"] = env

with open(config_path, "w") as f:
    json.dump(config, f, indent=4)
print(f"[*] Runsc config updated at {config_path}")
