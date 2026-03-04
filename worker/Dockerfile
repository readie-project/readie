FROM golang:1.22-alpine AS go-builder
WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN go build -o go-server .

FROM gcr.io/kaggle-images/python
WORKDIR /app

COPY --from=go-builder /app/go-server .

RUN python -c "import site; print(site.getsitepackages()[0])" > /app/sitepackages_path.txt

RUN mkdir /shared

ENV PORT=50052
ENV WORKER_DIR=/shared
ENV MAIN_NODE_URI=localhost:50051
ENV SITEPACKAGES_TXT_PATH=/app/sitepackages_path.txt

EXPOSE 50052

CMD ["./go-server"]