# Umbrella for the three components. Each keeps authority over its own build;
# this only coordinates them and owns the one thing they share: the protos.

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

.PHONY: all install protos protos-python protos-go protos-lint protos-fmt protos-breaking clean-protos \
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
