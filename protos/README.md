# protos

The protobuf definitions for every gRPC contract in Readie. The `.proto` files in
this directory are the single source of truth. The Go, Python and documentation
outputs are all generated from them.

## Services

| File | Service | Direction | Purpose |
| ---- | ------- | --------- | ------- |
| `proxy.proto` | `ProxyService` | client to router | `RequestExecution` is a bidirectional stream. The client sends an `ExecutionConfig` message first, then the pickled call in chunks. The router streams back the result. |
| `execution.proto` | `ExecutionService` | router to worker | `RequestExecution` is a bidirectional stream that carries one call to a chosen worker and container. The request names the checkpoint to restore. |
| `registry.proto` | `RegistryService` | worker to router | Four unary calls (`PostWorkerStatus`, `PostExecutorStatus`, `PostWorkerUtilization`, `PostExecutorUtilization`) with which workers report status, capacity and load. |
| `resources.proto` | none | shared | `ResourceKind` and `ResourceBudget`, the memory and GPU memory budgets carried in requests and responses. |

The generated reference with every message and field is published on the
documentation site, as the "Protobuf contracts" page. It is produced by
`make docs-protos` from the comments in these files, so write field comments as
documentation.

## Rules

Three rules are enforced by tooling and by tests.

1. **No `package` declaration.** Service names are bare on the wire, for example
   `/ProxyService/RequestExecution`. The Compose health check, the worker, and a
   router test all depend on these names. Adding a package would rename every
   method at once. `buf.yaml` disables the `PACKAGE_*` lint rules for this reason.
2. **Changes are additive only.** Add fields with new numbers. Never renumber or
   reuse a field number. When a field is retired, mark its number `reserved`, as
   `execution.proto` does for the old `cpu_alloc` and `gpu_alloc` fields.
   `buf breaking` checks every pull request against `main`. Field names are not
   on the wire, so renaming a misleading field is allowed.
3. **Generated code is committed.** After any edit, regenerate the stubs and commit
   the result. The `protos` CI job and the `proto-stubs-current` pre-commit hook
   fail when the committed stubs are stale.

The enum zero value is named `STATUS_UNKNOWN` instead of `STATUS_UNSPECIFIED`.
`buf.yaml` sets `enum_zero_value_suffix: _UNKNOWN` to match.

## Where each file is generated

`proxy.proto` is used only by Python code, because the Go worker never sees the
client-facing service.

| Output | Directory | Protos |
| ------ | --------- | ------ |
| Go (worker) | `worker/proto/` | `execution`, `registry`, `resources` |
| Python (router) | `router/src/readie_router/proto/` | all four |
| Python (client SDK) | `pkg/src/readie/_proto/` | `proxy`, `resources` |
| Documentation | `docs/docs/architecture/protos.md` | all four |

## Workflow

Run these from the repository root.

```sh
make protos            # regenerate the Go and Python stubs
make protos-lint       # buf lint and buf format check
make protos-fmt        # format the .proto files in place
make protos-breaking   # buf breaking, against the main branch
make docs-protos       # regenerate the documentation page
```

`make protos` needs `protoc` and the pinned generator plugins. `make -C worker tools`
installs them. `make docs-protos` also needs `protoc-gen-doc`, which
`make docs-tools` installs. `buf` is required for the lint, format and breaking
checks.

After a change, check that:

- `make protos` and `make docs-protos` leave no uncommitted diff.
- `make protos-lint` and `make protos-breaking` pass.
- `make test` passes in the router, the client SDK and the worker.

## Related

- `buf.yaml` at the repository root holds the lint and breaking-change rules.
- The wire protocol between the worker and the executor inside a sandbox is not
  protobuf. It is a length-prefixed binary framing described in `executor/README.md`.
