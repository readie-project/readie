# Umbrella for the five components. Each keeps authority over its own build;
# this coordinates them and owns the two things they share: the protos, and
# building a worker image with checkpoints baked in.

PROTO_SRC   := protos
PROTO_FILES := execution.proto proxy.proto registry.proto

# Router needs all three (it bridges clients to workers); the client needs only
# the client-facing one.
ROUTER_PKG   := crfs_router.proto
ROUTER_OUT   := router/src
ROUTER_PROTOS := execution.proto proxy.proto registry.proto

CLIENT_PKG    := crfs._proto
CLIENT_OUT    := pkg/src
CLIENT_PROTOS := proxy.proto

STAGE := .build/proto-stage

# --- generation --------------------------------------------------------------
# Where the pipeline writes its captured checkpoints, and where the worker
# Dockerfile copies them from.
ARTIFACTS_DIR   := worker/artifacts
ROOTFS_IMAGE    := crfs-executor-rootfs:latest
PIPELINE_IMAGE  := crfs-pipeline:latest
WORKER_BASE     := crfs-worker-base:latest
WORKER_IMAGE    := crfs-worker
# Overridable so an experiment can be tagged something meaningful.
TAG             ?= $(shell date -u +%Y%m%d-%H%M%S)

.PHONY: all install protos protos-python protos-go protos-lint protos-fmt protos-breaking clean-protos \
        rootfs-image worker-base pipeline-image capture worker-image generation clean-artifacts \
        router-% pkg-% executor-% pipeline-% worker-% lint type test help

all: protos lint type test ## Generate, check and test everything

install: ## Sync every Python virtualenv from its lockfile
	@$(MAKE) --no-print-directory -C router install
	@$(MAKE) --no-print-directory -C pkg install
	@$(MAKE) --no-print-directory -C executor install
	@$(MAKE) --no-print-directory -C pipeline install
	@$(MAKE) --no-print-directory -C worker install

# ---------------------------------------------------------------------------
# Protos
#
# One source of truth in protos/. Generated code is committed, as the worker's
# is, so a checkout builds without protoc.
#
# The staging directory is load-bearing. protoc derives a generated module's
# import from the proto's path relative to -I, so compiling protos/proxy.proto
# directly emits a bare `import proxy_pb2`, which only resolves if the package
# directory happens to be on sys.path. Staging the file at
# <pkg>/<path>/proxy.proto instead makes it emit
# `from crfs_router.proto import proxy_pb2`, which resolves properly from an
# installed package.
# ---------------------------------------------------------------------------
protos: protos-python protos-go ## Regenerate all gRPC stubs

protos-lint: ## Lint and format-check the protos
	buf lint
	buf format --diff --exit-code

protos-fmt: ## Format the protos in place
	buf format -w

protos-breaking: ## Check the protos for breaking changes against main
	buf breaking --against '.git#branch=main'

protos-python: ## Regenerate the router and client Python stubs
	@$(MAKE) --no-print-directory _stage_and_generate \
		PKG="$(ROUTER_PKG)" OUT="$(ROUTER_OUT)" PROTOS="$(ROUTER_PROTOS)"
	@$(MAKE) --no-print-directory _stage_and_generate \
		PKG="$(CLIENT_PKG)" OUT="$(CLIENT_OUT)" PROTOS="$(CLIENT_PROTOS)"

# PKG is a dotted package path; OUT is the src root it lives under.
_stage_and_generate:
	@set -e; \
	pkg_path=$$(printf '%s' "$(PKG)" | tr '.' '/'); \
	stage="$(STAGE)/$$pkg_path"; \
	rm -rf "$(STAGE)"; mkdir -p "$$stage"; \
	for p in $(PROTOS); do cp "$(PROTO_SRC)/$$p" "$$stage/"; done; \
	mkdir -p "$(OUT)/$$pkg_path"; \
	echo "  generating $(PKG) -> $(OUT)/$$pkg_path"; \
	cd router && uv run python -m grpc_tools.protoc \
		-I "../$(STAGE)" \
		--python_out="../$(OUT)" \
		--pyi_out="../$(OUT)" \
		--grpc_python_out="../$(OUT)" \
		$$(for p in $(PROTOS); do printf '%s ' "$$pkg_path/$$p"; done); \
	cd ..; \
	rm -rf "$(STAGE)"

protos-go: ## Regenerate the worker's Go stubs
	@$(MAKE) --no-print-directory -C worker proto

clean-protos: ## Remove generated Python stubs
	rm -rf router/src/crfs_router/proto/*_pb2*.py router/src/crfs_router/proto/*.pyi
	rm -rf pkg/src/crfs/_proto/*_pb2*.py pkg/src/crfs/_proto/*.pyi

# ---------------------------------------------------------------------------
# Generation: capture checkpoints, then bake them into a worker image.
#
# Two phases, because `docker build` cannot capture a checkpoint: runsc needs
# privileged namespace access, which a stock BuildKit builder will not grant
# without a builder created for `--allow security.insecure`. Running the
# pipeline as a privileged container is portable; that is not.
#
# gVisor is amd64-only and dies under emulation, so `capture` only works on an
# amd64 Linux host. It fails loudly rather than leaving a checkpointless image
# that looks like a successful build.
# ---------------------------------------------------------------------------
generation: capture worker-image ## Capture checkpoints and bake them into a worker image
	@echo
	@echo "  image:  $(WORKER_IMAGE):$(TAG)"
	@echo "  also:   $(WORKER_IMAGE):latest"
	@echo "  run it: docker compose up -d"

rootfs-image: ## Build the executor root filesystem image
	@echo "==> [1/3] rootfs image"
	docker build -t $(ROOTFS_IMAGE) -f rootfs/Dockerfile .

pipeline-image: rootfs-image ## Build the offline pipeline image
	@echo "==> [2/3] pipeline image"
	docker build -t $(PIPELINE_IMAGE) -f pipeline/Dockerfile .

capture: pipeline-image ## Capture checkpoints into $(ARTIFACTS_DIR)
	@echo "==> [3/3] capturing checkpoints (privileged; needs amd64 gVisor)"
	@rm -rf $(ARTIFACTS_DIR)/manifest.json $(ARTIFACTS_DIR)/checkpoints
	@mkdir -p $(ARTIFACTS_DIR)
	docker run --rm \
		--privileged \
		--security-opt apparmor=unconfined \
		--security-opt seccomp=unconfined \
		-v "$(CURDIR)/$(ARTIFACTS_DIR):/app/executor" \
		$(PIPELINE_IMAGE) build
	@test -f $(ARTIFACTS_DIR)/manifest.json \
		|| { echo "capture produced no manifest; refusing to build a checkpointless image" >&2; exit 1; }
	@echo "==> captured: $$(ls $(ARTIFACTS_DIR)/checkpoints | tr '\n' ' ')"

worker-base: rootfs-image ## Build the worker base: runsc plus the root filesystem
	@echo "==> worker base image (only needed when the rootfs or runsc changes)"
	docker build -t $(WORKER_BASE) -f worker/Dockerfile.base .

worker-image: ## Build the worker image around whatever is in $(ARTIFACTS_DIR)
	@docker image inspect $(WORKER_BASE) >/dev/null 2>&1 \
		|| $(MAKE) --no-print-directory worker-base
	@echo "==> worker image $(WORKER_IMAGE):$(TAG)"
	docker build \
		-t $(WORKER_IMAGE):$(TAG) -t $(WORKER_IMAGE):latest \
		--build-arg ARTIFACTS_DIR=$(ARTIFACTS_DIR) \
		-f worker/Dockerfile .

clean-artifacts: ## Discard captured checkpoints, keeping the placeholder
	rm -rf $(ARTIFACTS_DIR)/manifest.json $(ARTIFACTS_DIR)/checkpoints

# ---------------------------------------------------------------------------
# Per-component passthrough: `make router-test`, `make worker-lint`, …
# ---------------------------------------------------------------------------
router-%:
	@$(MAKE) --no-print-directory -C router $*

pkg-%:
	@$(MAKE) --no-print-directory -C pkg $*

executor-%:
	@$(MAKE) --no-print-directory -C executor $*

pipeline-%:
	@$(MAKE) --no-print-directory -C pipeline $*

worker-%:
	@$(MAKE) --no-print-directory -C worker $*

# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------
lint: protos-lint ## Lint every component
	@$(MAKE) --no-print-directory -C router lint
	@$(MAKE) --no-print-directory -C pkg lint
	@$(MAKE) --no-print-directory -C executor lint
	@$(MAKE) --no-print-directory -C pipeline lint
	@$(MAKE) --no-print-directory -C worker lint

type: ## Type-check the Python components
	@$(MAKE) --no-print-directory -C router type
	@$(MAKE) --no-print-directory -C pkg type
	@$(MAKE) --no-print-directory -C executor type
	@$(MAKE) --no-print-directory -C pipeline type

test: ## Test every component
	@$(MAKE) --no-print-directory -C router test
	@$(MAKE) --no-print-directory -C pkg test
	@$(MAKE) --no-print-directory -C executor test
	@$(MAKE) --no-print-directory -C pipeline test
	@$(MAKE) --no-print-directory -C worker test

# No up/down/build targets. They were one-line aliases for the equivalent
# docker compose commands, which added a layer of indirection and made `make`
# look like a prerequisite for running the system. It is not: compose runs the
# stack, and this file covers the development loop compose cannot.

help: ## List available targets
	@grep -hE '^[a-zA-Z_%-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
