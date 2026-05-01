FROM golang:1.24-bookworm AS go-builder

# Install GVisor dependencies
RUN sudo apt-get update && sudo apt-get install -y apt-transport-https ca-certificates curl gnupg
RUN curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
RUN echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
RUN sudo apt-get update && sudo apt-get install -y runsc

# Build the Go server
WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN go build -o go-server ./cmd/main.go

RUN wget -qO- https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_x86_64.tar.gz | tar -xz -C /usr/local/bin

FROM gcr.io/kaggle-images/python
WORKDIR /app

COPY --from=go-builder /app/go-server .

RUN python -c "import site; print(site.getsitepackages()[0])" > /app/sitepackages_path.txt

RUN mkdir /shared

ENV SERVICE_NAME=worker
ENV PORT=50052
ENV WORKER_DIR=/shared
ENV ROUTER_URI=router:50051
ENV SITEPACKAGES_TXT_PATH=/app/sitepackages_path.txt
ENV APP_ENV=production

EXPOSE 50052

CMD ["./go-server"]