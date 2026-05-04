import os
import subprocess
from .types import Checkpoint

checkpoints_dir = os.path.join(os.path.dirname(__file__), "checkpoints")


def build_checkpoint(id: str, executor_code: str, executor_path: str, checkpoint: Checkpoint):
    imports = "\n".join([f"import {imp}" for imp in checkpoint["imports"]])

    # Add datasets, models, tokenizers to the code as variables

    executor_code = imports + "\n\n" + executor_code

    with open(executor_path, "w") as f:
        f.write(executor_code)

    process = subprocess.Popen(
        f"runsc --ignore-cgroups --network=none run {id}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    checkpoint_path = os.path.join(checkpoints_dir, id)
    os.mkdir(checkpoint_path)

    # Wait for resources to be loaded
    for line in iter(process.stdout.readline, ""):
        print(line.strip())
        if "READY_FOR_CHECKPOINT" in line:
            # Checkpoint the container
            subprocess.run(
                f"runsc checkpoint --image-path={checkpoint_path} {id}", shell=True, check=True, capture_output=True, text=True)
            break

    # Delete the container
    subprocess.run(f"runsc delete {id}", shell=True,
                   check=True, capture_output=True, text=True)
