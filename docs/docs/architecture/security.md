---
title: Security
sidebar_position: 10
description: Process for reporting a vulnerability, and the trust model that defines which behavior Readie treats as expected.
---

Readie is a research platform for running untrusted Python on shared machines. This page summarizes how to report a vulnerability and what Readie trusts. For the full design, see [Security model](/docs/architecture/security-model).

## Report a vulnerability

Report vulnerabilities through GitHub private reporting. Do not open a public issue for anything that can be exploited.

1. On the repository, open **Security**.
2. Select **Report a vulnerability**. GitHub creates a private security advisory that only the maintainers can see.
3. Describe what you did, what happened, and which component is affected (router, worker, executor, client, or pipeline).

A proof of concept helps. A working exploit is not required.

## Trust model

A call passes through four places:

```text
your process --> router --> worker --> sandbox (executor + your code)
```

Readie has two trust boundaries. Both are part of the design and are not oversights.

### Boundary 1: the client trusts the cluster

The client unpickles the result it receives, and unpickling can run arbitrary code. If the router or worker is compromised, it can run code in the client process, with access to the files and credentials of that process.

Validating the data first cannot prevent this, because sending live Python objects requires unpickling. The client contains the risk: result decoding goes through a `ResultCodec` interface, so a deployment can supply a safer codec. The cost is that the client can no longer return arbitrary objects.

### Boundary 2: the sandbox runs arbitrary code

Running user code is the purpose of the product, and [gVisor](/docs/concepts/glossary) contains it. gVisor handles the system calls of the code in user space and does not pass them directly to the host kernel. The following controls apply in addition:

- The root filesystem is shared and read-only. Each sandbox has a private writable layer, so one run cannot change the state from which the next run starts.
- CPU, memory, and process limits apply to each container.
- Each sandbox has one interpreter and runs one execution at a time, so the code of two users never shares an address space.

The remaining risk is a gVisor escape. The gVisor version is pinned in one place, and checkpoints depend on it, so upgrading it requires rebuilding the checkpoints. Monitor the [gVisor security advisories](https://github.com/google/gvisor/security/advisories).

## Executor-reported telemetry

The executor does not contact the collector. It sends a bounded JSON timing
trailer over the existing sandbox socket after its result. The worker validates
and reconstructs sampled spans after gRPC completes, with fixed names and outcomes
and no user payload or exception text. User code shares the reporting process,
so these observations are untrusted. See [Executor telemetry](/docs/architecture/security-model#executor-telemetry).

## Client-reported telemetry

Optional completion reports are untrusted client assertions. The router requires a per-execution capability, validates a bounded report, and creates a synthetic child span marked as client-reported. Clients cannot choose the server trace identity or access the collector. Reports contain timing and fixed error categories, not messages, tracebacks, arguments, or results. A reporting capability does not prove that an observation is true. See [Client telemetry](/docs/architecture/security-model#client-telemetry) for authentication, expiry, and ingress limits.

## Expected behavior

The following behaviors are not vulnerabilities:

- A remote function reads the filesystem of its own container. This is the intended surface of the sandbox: a shared read-only base plus a private layer.
- A submitted function exhausts resources. Container limits and the execution deadline bound this, and both are configurable. Denial of service by an authenticated user is an operational matter.

## What's next

- [Security model](/docs/architecture/security-model) describes the full design.
- If you run the service yourself, see the [hardening checklist](/docs/contributing/deploy-with-tls#hardening-checklist).
