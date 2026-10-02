---
title: Change protos
sidebar_position: 3
description: Rules for editing the gRPC contracts, the commands that regenerate the committed stubs, and a worked example of adding a field.
---

The `.proto` files in `protos/` define every message that crosses a process boundary. A [protobuf](/docs/guides/glossary) file describes messages and gRPC services, and a code generator turns it into Python and Go classes. The generated files are committed, so a fresh checkout builds without the generator. A single edit changes Python and Go code at once, so follow the rules and steps on this page.

## Prerequisites

- The tools from [Set up your environment](/docs/contributing/setup), in particular `buf`.
- The Go generators, installed once with `make -C worker tools`. Their versions are pinned in `worker/Makefile`.
- A local `main` branch, which the `protos-breaking` target compares against. If none exists, run `git fetch origin main:main`.

Python stubs are generated with `grpc_tools.protoc`, run through the `uv` environment of the router. Go stubs are generated with `protoc`, `protoc-gen-go`, and `protoc-gen-go-grpc`. Generator versions are pinned because different versions produce different bytes, and CI compares bytes.

## Rules

1. **Do not add a `package` line.** Service names are bare on the wire, for example `/ProxyService/RequestExecution`. The container health check, the worker, and a router test depend on those names. Adding a package renames every method at once. `buf.yaml` switches off the lint rules that would otherwise require a package.
2. **Only add fields. Never reuse or renumber them.** The field number is what travels on the wire. If a number changes, old messages are silently read as the wrong field. Add new fields with new numbers. To retire a field, mark its number `reserved`. `proxy.proto` already does this for the old field 3.
3. **Regenerate and commit the stubs.** Run `make protos` and commit the result. CI fails if the committed files differ from freshly generated ones.
4. **Treat `proxy.proto` as Python only.** It is generated for the router and the client, never for Go, because the worker does not use the client-facing service.

## Generated output locations

`make protos` generates code into three locations:

| Proto | Router (`router/src/readie_router/proto`) | Client (`pkg/src/readie/_proto`) | Worker (`worker/proto`) |
| --- | --- | --- | --- |
| `proxy.proto` | yes | yes | no |
| `execution.proto` | yes | no | yes |
| `registry.proto` | yes | no | yes |
| `resources.proto` | yes | yes | yes |

## Commands

```bash
make protos            # regenerate Python and Go stubs
make protos-python     # only the router and client stubs
make protos-go         # only the worker stubs
make protos-fmt        # buf format -w
make protos-lint       # buf lint, plus a format check
make protos-breaking   # buf breaking, compared with main
```

## Example: add an optional field

The following steps add a field that lets the client tell the router a call is latency sensitive. The field is invented for illustration, and the steps are real.

1. Edit the proto. In `protos/proxy.proto`, `ExecutionConfig` uses numbers 1 to 4, so the next free number is 5.

   ```proto
   message ExecutionConfig {
     // ...existing fields 1 to 4...

     // True if the caller wants the fastest start, even at higher cost.
     bool latency_sensitive = 5;
   }
   ```

   A new `bool` defaults to `false` when absent. Old clients that never set it keep working, and an old router ignores the unknown field. This property is what additive means.

2. Regenerate the stubs.

   ```bash
   make protos
   ```

   In `git status`, `proxy_pb2.py` and `proxy_pb2.pyi` change under both `router/` and `pkg/`. No Go files change, because `proxy.proto` is not generated for Go.

3. Use the field. Set it in the client, in `pkg/src/readie/`, and read it in the router, in `router/src/readie_router/`. If the worker also needs the field, add a field to `execution.proto`, set it in the router, and run `make protos` again. The Go stubs then change.

4. Add tests. One test sets the field and checks that the receiving side sees it. A second test covers the absent field, which is what an older client sends.

5. Check the contract.

   ```bash
   buf lint
   buf format -w
   make protos-breaking
   make lint type test
   ```

6. Commit the proto, the generated files, and the code together. In the pull request, select "Proto field added" in the Contract changes section. See [Pull requests](/docs/contributing/pull-requests).

## What CI checks

Two jobs watch the protos:

- **protos lint** runs `buf lint` and `buf format --diff --exit-code`. On pull requests it also runs `buf breaking` against `main`.
- **generated stubs are current** installs the pinned generators, runs `make protos`, and fails if `git diff` is not empty.

The `proto-stubs-current` pre-commit hook runs the same regeneration locally when a proto change is committed. For all CI jobs, see [Pull requests](/docs/contributing/pull-requests).

## What's next

- [Protos reference](/docs/architecture/protos), for field-by-field documentation
- [Changing the executor protocol](/docs/contributing/changing-executor-protocol)
- [Pull requests](/docs/contributing/pull-requests)
