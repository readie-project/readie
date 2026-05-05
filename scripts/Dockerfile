FROM gcr.io/kaggle-images/python AS rootfs_image
FROM python:3.12-slim

# Install GVisor
RUN apt-get update && apt-get install -y apt-transport-https ca-certificates curl gnupg
RUN curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
RUN echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | tee /etc/apt/sources.list.d/gvisor.list > /dev/null
RUN apt-get update && apt-get install -y runsc

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r dataset/requirements.txt

RUN mkdir -p executorfs/rootfs
RUN mkdir -p executor/checkpoints

COPY --from=rootfs_image / ./executorfs/rootfs

COPY ./executor/ ./executorfs/rootfs/app/executor/

ENV BASE_DIR="/app/executorfs"
ENV EXECUTOR_DIR="/app/executor"
ENV BUILD_CHECKPOINTS_NAME="checkpoints.json"
ENV CHECKPOINTS_DIR_NAME="checkpoints"
ENV EXECUTOR_NAME="app.py"

# Needs to correspond to the rootfs_image above
ENV ROOTFS_BINARY_PATH="/bin"
ENV ROOTFS_PYTHONPATH="/lib/python3.12/dist-packages"

RUN python -u setup.py

ENTRYPOINT ["python", "-u", "main.py"]