# Umbrella for the five components. Each keeps authority over its own build;
# this coordinates them and owns the two things they share: the protos, and
# building a worker image with checkpoints baked in.

PROTO_SRC   := protos
PROTO_FILES := execution.proto proxy.proto registry.proto resources.proto

# Router needs all of them (it bridges clients to workers); the client needs the
# client-facing one plus resources.proto, which proxy.proto imports.
ROUTER_PKG   := crfs_router.proto
ROUTER_OUT   := router/src
ROUTER_PROTOS := execution.proto proxy.proto registry.proto resources.proto

CLIENT_PKG    := crfs._proto
CLIENT_OUT    := pkg/src
CLIENT_PROTOS := proxy.proto resources.proto

STAGE := .build/proto-stage

# --- generation --------------------------------------------------------------
# A generation is per-flavor: `make generation FLAVOR=cpu` and `FLAVOR=gpu`
# produce fully independent artifact dirs and images, so one never clobbers the
# other's checkpoints. Everything below hangs off FLAVOR.
FLAVOR          ?= cpu
# Where this flavor's captured checkpoints and catalogue go, and where worker-base
# copies them from. Per-flavor so the two generations coexist.
ARTIFACTS_DIR   := pipeline/out-$(FLAVOR)
# The per-flavor catalogues the router mounts (as <flavor>.json).
CATALOGUE_DIR   := catalogues
# The pipeline's Dockerfile defines the rootfs, the capture tool and the base a
# worker runs on — everything a checkpoint's validity is bound to, in one file.
# The worker's own image is worker/Dockerfile, built from ./worker.
PIPELINE_DOCKERFILE := pipeline/Dockerfile
WORKER_DOCKERFILE   := worker/Dockerfile
PIPELINE_IMAGE  := crfs-pipeline-$(FLAVOR):latest
WORKER_BASE     := crfs-worker-base-$(FLAVOR):latest
WORKER_IMAGE    := crfs-worker-$(FLAVOR)

# The rootfs base image. A cpu generation uses the plain Kaggle image; a gpu
# generation passes gcr.io/kaggle-gpu-images/python (CUDA + the GPU stack). The
# Makefile selects it per FLAVOR.
# A gpu generation uses the Kaggle GPU rootfs and captures under nvproxy with the
# host's GPUs attached; a cpu generation uses the plain image and no devices.
ifeq ($(FLAVOR),gpu)
ROOTFS_BASE_IMAGE := gcr.io/kaggle-gpu-images/python
CAPTURE_GPU_FLAGS := --gpus all
else
ROOTFS_BASE_IMAGE := gcr.io/kaggle-images/python
CAPTURE_GPU_FLAGS :=
endif
# Overridable so an experiment can be tagged something meaningful.
TAG             ?= $(shell date -u +%Y%m%d-%H%M%S)

# Planner knobs
# greedy (weighted set cover, the default) | fixed (a configured package set)
CRFS_PLANNER := greedy
# Upper bound on how many checkpoints a plan may emit.
CRFS_MAX_CHECKPOINTS := 8
# Per-checkpoint size budget, in MB.
CRFS_SIZE_BUDGET_MB := 2048.0
# Size-vs-time weight (seconds per MB): the planner adds a package while it saves
# more than alpha*size. MUST match the router's ALPHA. Default 0.002.
CRFS_ALPHA := 0.002

.PHONY: all install protos protos-python protos-go protos-lint protos-fmt protos-breaking clean-protos \
        worker-base pipeline-image capture worker-image generation clean-artifacts \
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
	for p in $(PROTOS); do \
		sed -i.bak "s|import \"$$p\"|import \"$$pkg_path/$$p\"|g" "$$stage"/*.proto; \
	done; \
	rm -f "$$stage"/*.bak; \
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
# Generation: capture checkpoints, bake them into the base, put a worker on it.
#
# Capture is its own phase because `docker build` cannot capture a checkpoint:
# runsc needs privileged namespace access, which a stock BuildKit builder will not
# grant without a builder created for `--allow security.insecure`. Running the
# pipeline as a privileged container is portable; that is not.
#
# gVisor is amd64-only and dies under emulation, so `capture` only works on an
# amd64 Linux host. It fails loudly rather than leaving a checkpointless image
# that looks like a successful build.
#
# To redeploy a worker code change, run `worker-image` alone: the base already has
# the rootfs and the checkpoints, so it is a Go build and nothing else.
# ---------------------------------------------------------------------------
# Three ordered steps rather than prerequisites. The base bakes what capture
# wrote, so the order is load-bearing and `make -j` is free to reorder
# prerequisites. worker-image deliberately does not depend on capture: that is
# what keeps the redeploy loop from re-capturing.
generation: ## Capture checkpoints, bake them into the base, build a worker
	@$(MAKE) --no-print-directory capture
	@$(MAKE) --no-print-directory worker-base
	@$(MAKE) --no-print-directory worker-image
	@echo
	@echo "  image:  $(WORKER_IMAGE):$(TAG)"
	@echo "  also:   $(WORKER_IMAGE):latest"
	@echo "  run it: docker compose up -d"

pipeline-image: ## Build the offline pipeline image
	@echo "==> [1/3] pipeline image ($(FLAVOR))"
	docker build --target pipeline -t $(PIPELINE_IMAGE) \
		--build-arg ROOTFS_BASE_IMAGE=$(ROOTFS_BASE_IMAGE) \
		-f $(PIPELINE_DOCKERFILE) .

# One run, not two: `capture` plans and captures in the same container because
# the plan writes the bundle's config.json into the image's own filesystem, which
# a second container would not see. FLAVOR reaches the pipeline as an env var so a
# gpu capture runs under nvproxy and records the matching fingerprint.
capture: pipeline-image ## Capture checkpoints into $(ARTIFACTS_DIR)
	@echo "==> [2/3] capturing $(FLAVOR) checkpoints (privileged; needs amd64 gVisor)"
	@rm -rf $(ARTIFACTS_DIR)/manifest.json $(ARTIFACTS_DIR)/checkpoints $(ARTIFACTS_DIR)/catalogue.json
	@mkdir -p $(ARTIFACTS_DIR)
	docker run --rm -it \
		--privileged \
		$(CAPTURE_GPU_FLAGS) \
		--security-opt apparmor=unconfined \
		--security-opt seccomp=unconfined \
		-e FLAVOR=$(FLAVOR) \
		-e CRFS_PLANNER=$(CRFS_PLANNER) \
		-e CRFS_MAX_CHECKPOINTS=$(CRFS_MAX_CHECKPOINTS) \
		-e CRFS_SIZE_BUDGET_MB=$(CRFS_SIZE_BUDGET_MB) \
		-e CRFS_ALPHA=$(CRFS_ALPHA) \
		-v "$(CURDIR)/$(ARTIFACTS_DIR):/app/executor" \
		$(PIPELINE_IMAGE) capture
	@test -f $(ARTIFACTS_DIR)/manifest.json \
		|| { echo "capture produced no manifest; refusing to build a checkpointless image" >&2; exit 1; }
	@mkdir -p $(CATALOGUE_DIR)
	@test -f $(ARTIFACTS_DIR)/catalogue.json && cp $(ARTIFACTS_DIR)/catalogue.json $(CATALOGUE_DIR)/$(FLAVOR).json || true
	@echo "==> captured: $$(ls $(ARTIFACTS_DIR)/checkpoints | tr '\n' ' ')"
	@echo "==> catalogue: $(CATALOGUE_DIR)/$(FLAVOR).json (mount this on the router)"

# Says what it baked, because building this from an empty $(ARTIFACTS_DIR) yields
# a base that works and serves nothing but cold starts — which otherwise looks
# exactly like a normal build.
worker-base: ## Build the base a worker runs on: runsc, rootfs, checkpoints
	@echo "==> [3/3] worker base image ($(FLAVOR)): runsc, rootfs, and $$(ls $(ARTIFACTS_DIR)/checkpoints 2>/dev/null | wc -l | tr -d ' ') checkpoint(s)"
	docker build --target worker-base -t $(WORKER_BASE) \
		--build-arg ROOTFS_BASE_IMAGE=$(ROOTFS_BASE_IMAGE) \
		--build-arg ARTIFACTS_DIR=$(ARTIFACTS_DIR) \
		-f $(PIPELINE_DOCKERFILE) .

# worker/Dockerfile is `FROM $(WORKER_BASE)`, so the base has to exist as an image
# rather than be built on the way. That is what makes the rootfs layer inherited
# by digest instead of recopied; see worker/Dockerfile. The context is ./worker —
# nothing outside it is needed, which is what makes this build seconds long.
worker-image: ## Build the worker image on top of $(WORKER_BASE)
	@docker image inspect $(WORKER_BASE) >/dev/null 2>&1 \
		|| $(MAKE) --no-print-directory worker-base
	@echo "==> worker image $(WORKER_IMAGE):$(TAG)"
	docker build \
		-t $(WORKER_IMAGE):$(TAG) -t $(WORKER_IMAGE):latest \
		--build-arg BASE_IMAGE=$(WORKER_BASE) \
		--build-arg WORKER_FLAVOR=$(FLAVOR) \
		-f $(WORKER_DOCKERFILE) ./worker

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
