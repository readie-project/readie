FROM gcr.io/kaggle-images/python

# Install GVisor
RUN sudo apt-get update && sudo apt-get install -y apt-transport-https ca-certificates curl gnupg
RUN curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
RUN echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
RUN sudo apt-get update && sudo apt-get install -y runsc

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

# Needs to match base image name above
ENV ROOTFS_IMAGE_NAME="gcr.io/kaggle-images/python"
ENV ROOTFS_BINARY_PATH="/bin"
ENV ROOTFS_PYTHONPATH="/lib/python3.12/dist-packages"

ENTRYPOINT ["python", "-u", "main.py"]