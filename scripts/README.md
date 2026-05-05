docker build --progress=plain -f Dockerfile -t checkpoint-create .

mkdir -p executor/checkpoints

docker run -it --rm   --privileged   --security-opt apparmor=unconfined   -v ./executor/checkpoints:/app/executor/checkpoints   -v /var/run/docker.sock:/var/run/docker.sock   --name mycontainer   checkpoint-create