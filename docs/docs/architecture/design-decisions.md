---
title: Design decisions
sidebar_position: 8
description: Records of deliberate design choices in Readie, with the context, the decision, and the consequences of each.
---

Several parts of Readie look unusual until the reason is known. This page records those choices as architecture decision records, so that a deliberate decision can be told apart from an oversight before anyone changes it.

Each record lists its status, the context that forced the decision, the decision, and the consequences. All records describe decisions that are in force.

## 1. Protos declare no package

- Status: Accepted

### Context

gRPC names every method as `/<package>.<Service>/<Method>`. The router, worker, and client share the `.proto` files in `protos/`.

### Decision

No file declares a `package`, so method names are bare, such as `/ProxyService/RequestExecution`. The linter rule that normally requires a package is turned off in `buf.yaml`.

### Consequences

- The Docker health check, the worker, and a router test depend on the bare names.
- Adding a package would rename every method on the wire at once, so no file adds one.

## 2. Generated code is committed

- Status: Accepted

### Context

The Python and Go gRPC code is generated from the `.proto` files.

### Decision

The generated files live in the repository. After a change to a proto, run `make protos` and commit the result.

### Consequences

- A fresh checkout builds without `protoc`.
- The generated files can go stale. A CI job regenerates them and fails on any difference, and a pre-commit hook does the same locally.
- `proxy.proto` is generated for Python only, because the Go worker never uses it.

## 3. Proto changes are additive only

- Status: Accepted

### Context

Messages cross process boundaries, and old and new versions of a component can run together.

### Decision

New fields get new numbers. Numbers are never reused or renumbered. `buf breaking` compares each pull request against `main`. Field names can change, because they are not on the wire.

### Consequences

- Protos can grow but cannot be cleaned up.
- A renumbered field would silently change the meaning of messages already in flight, so the rule justifies the clutter.

See [Changing protos](/docs/contributing/changing-protos).

## 4. Python 3.12 on the client and executor, checked at runtime

- Status: Accepted

### Context

The client pickles a function on the user's machine, and the executor unpickles it inside the sandbox. Pickled code is only safe to move between interpreters of the same minor Python version. The executor runs on the Python version of the sandbox filesystem, which is 3.12.

### Decision

The client (`pkg`) and the executor require exactly Python 3.12 (`requires-python = "~=3.12.0"`). `readie.Client()` also checks the interpreter when it is created and raises `IncompatiblePythonError` unless the interpreter is 3.12. The router and the pipeline use Python 3.13, because they control their own images.

### Consequences

- Users on other versions receive a clear error up front instead of a confusing failure inside the sandbox.
- If the sandbox Python version changes, three places must change together: `REQUIRED_PYTHON` in `pkg/src/readie/_compat.py` and the `requires-python` fields in `pkg` and `executor`.

See [Errors](/docs/guides/errors).

## 5. One alpha trades checkpoint size against import time

- Status: Accepted

### Context

A larger checkpoint saves more import time but costs more to store and restore. The pipeline planner and the router selector must weigh that trade the same way. Otherwise the router chooses against costs that the checkpoints were not built for.

### Decision

`alpha` is measured in seconds per MB. The planner uses `READIE_ALPHA` as its starting weight. After capturing, the pipeline fits `alpha` from the measured restore times and writes the value into `catalogue.json`. The router reads `alpha` from the catalogue and uses a built-in fallback only if the field is missing. The router has no `alpha` setting of its own.

### Consequences

- The catalogue file carries the shared constant, instead of two matching environment variables.
- After a change to `READIE_ALPHA` and a rebuild, the router picks up the new measured value when it loads the new catalogue.

See [Cost model](/docs/concepts/cost-model).

## 6. The executor protocol is implemented twice with a golden fixture

- Status: Accepted

### Context

The executor is written in Python and the worker in Go. Both must speak the same byte format.

### Decision

Each side has its own implementation. A shared file, `executor/tests/data/frames.golden.json`, holds example payloads and their encoded bytes, and both test suites read it. The manifest records a protocol version, and the worker refuses artifacts with a version that it does not implement.

### Consequences

- A change to the format must land in both languages at once, and the fixture must be regenerated.
- Drift appears as a failing test, not as a hung request weeks later.

See [Executor protocol](/docs/architecture/executor-protocol).

## 7. Constructor injection and Protocol seams

- Status: Accepted

### Context

Components talk to things that are hard to run in tests: gVisor, the network, and other processes.

### Decision

Classes receive their collaborators through their constructors. Boundaries are `typing.Protocol` classes in Python and interfaces in Go, not abstract base classes. For example, the client takes a `ResultCodec` and a transport, and the worker's container manager takes a sandbox runtime, a filesystem, and a clock.

### Consequences

- Tests use fakes, so most of the suite runs anywhere, including machines without gVisor.
- A deployment can replace a part, such as the result decoder, without subclassing.
- Start-up requires more wiring code.

## 8. Deliberate omissions

The following choices can look like gaps. Each is deliberate and in force. The section lists the decision and the consequences of each choice.

### Router state is in memory

- Status: Accepted
- Decision: The router keeps its state in memory and has no datastore dependency.
- Consequences: If the router restarts, sessions are lost and workers register again. A database would add a network hop to every placement decision.

### The root filesystem is the full base image

- Status: Accepted
- Decision: The root filesystem is the full base image, whatever the planner picks. The filesystem lives in the worker base image, and the worker image inherits it.
- Consequences: Rebuilding a worker does not mean rebuilding tens of gigabytes.

### Checkpoints are baked in, not mounted

- Status: Accepted
- Context: A checkpoint restores only into the exact filesystem and gVisor release that it was captured with.
- Decision: Checkpoints are part of the base image. New checkpoints require a new base image (`make generation`).
- Consequences: A single image guarantees the pairing of checkpoint, filesystem, and gVisor release, and there is one artifact to deploy and nothing to mount incorrectly. The image is very large. Redeploying only worker code is fast (`make worker-image`). For sizes, see [Building checkpoints](/docs/contributing/build-checkpoints).

### The client unpickles router-supplied bytes

- Status: Accepted
- Decision: The client unpickles the result bytes that the router supplies.
- Consequences: This is an inherent code-execution surface. It is isolated behind `ResultCodec` and described in the [security model](/docs/architecture/security-model).

### gVisor requires an amd64 host

- Status: Accepted
- Decision: The sandbox path targets amd64 only, and the filesystem image is published for amd64 only.
- Consequences: The sandbox path cannot run on Apple Silicon or in ordinary CI. A passing CI run does not show that the sandbox path works.

## See also

- [Security model](/docs/architecture/security-model)
- [Executor protocol](/docs/architecture/executor-protocol)
- [Changing protos](/docs/contributing/changing-protos)
