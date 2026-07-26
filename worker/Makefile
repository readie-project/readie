MODULE     := github.com/illinoisdata/checkpoint-restore-for-serverless/worker
BINARY     := go-server
PROTO_DIR  := ../protos
PROTOS     := $(PROTO_DIR)/execution.proto $(PROTO_DIR)/registry.proto

# Pinned to the versions recorded in the committed proto/*.pb.go headers.
PROTOC_GEN_GO      := google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.11
PROTOC_GEN_GO_GRPC := google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.1
GOLANGCI_LINT      := github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.6.2

GO      ?= go
GOBIN   := $(shell $(GO) env GOPATH)/bin

.PHONY: all build run test test-short cover race lint fmt tidy vet proto tools docker clean help

all: fmt vet lint test build ## Run the full local pipeline

build: ## Build the worker binary
	CGO_ENABLED=0 $(GO) build -trimpath -ldflags="-s -w" -o $(BINARY) ./cmd/worker

run: ## Run the worker from source
	$(GO) run ./cmd/worker

test: ## Run all tests with the race detector
	$(GO) test -race -count=1 ./...

test-short: ## Skip slow/timing-sensitive tests
	$(GO) test -race -short -count=1 ./...

cover: ## Run tests and open a coverage report
	$(GO) test -race -count=1 -coverprofile=coverage.out -covermode=atomic ./...
	$(GO) tool cover -func=coverage.out | tail -n 1
	$(GO) tool cover -html=coverage.out -o coverage.html

vet: ## Run go vet
	$(GO) vet ./...

lint: ## Run golangci-lint
	@command -v golangci-lint >/dev/null 2>&1 || { echo "golangci-lint not found; run 'make tools'"; exit 1; }
	golangci-lint run ./...

fmt: ## Format the tree
	$(GO) fmt ./...

tidy: ## Tidy and verify go.mod/go.sum
	$(GO) mod tidy
	$(GO) mod verify

# NOTE: protos/*.proto deliberately declare no `package`, so the generated service
# names stay unqualified (/ExecutionService/RequestExecution). The router depends on
# this. Do not add a package declaration. proxy.proto is router-only and is not
# generated here.
proto: ## Regenerate the gRPC stubs from ../protos
	@command -v protoc >/dev/null 2>&1 || { echo "protoc not found (brew install protobuf)"; exit 1; }
	PATH="$(GOBIN):$$PATH" protoc -I $(PROTO_DIR) \
		--go_out=. --go_opt=module=$(MODULE) \
		--go-grpc_out=. --go-grpc_opt=module=$(MODULE) \
		$(PROTOS)

tools: ## Install the pinned code-generation and lint tools
	$(GO) install $(PROTOC_GEN_GO)
	$(GO) install $(PROTOC_GEN_GO_GRPC)
	$(GO) install $(GOLANGCI_LINT)

docker: ## Build the worker container image
	docker build -t worker:latest .

clean: ## Remove build artifacts
	rm -f $(BINARY) coverage.out coverage.html

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
