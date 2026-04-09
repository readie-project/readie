import docker
import os

client = docker.from_env()

IMAGE_TAG = "python-metadata-analyzer"

PACKAGES = ["numpy", "scikit-learn", "pandas", "matplotlib", "seaborn", "scipy", "torch", "transformers"]

with open("requirements.txt", "w") as f:
    for pkg in PACKAGES:
        f.write(f"{pkg}\n")

workdir = os.getcwd()

print(f"[*] Building environment...")
client.images.build(path=workdir, dockerfile="Dockerfile", rm=True, tag=IMAGE_TAG, quiet=False)
print(f"[*] Built Docker image with tag: {IMAGE_TAG}")

print(f"[*] Analyzing environment...")
run_output = client.containers.run(IMAGE_TAG, volumes={workdir: {'bind': '/output', 'mode': 'rw'}}, remove=True, detach=False)
print(run_output.decode("utf-8"))