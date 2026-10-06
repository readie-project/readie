---
title: Sandboxes
sidebar_position: 3
description: How Readie isolates each function in a gVisor sandbox, and the capabilities, network access, and limits the function has there.
---

A sandbox is an isolated environment with its own filesystem, process list, and network, and with fixed limits on memory, CPU, and process count. Readie builds sandboxes with gVisor, which prevents code inside from calling the host operating system directly. Every function runs in a sandbox, which allows Readie to run code that it did not write.

## Purpose

Readie runs Python functions that many different users send over the network. That code can perform almost any operation, and a sandbox limits the damage. Sandboxes also make checkpoints possible, because gVisor can save and restore a whole running sandbox.

## gVisor and runsc

gVisor is a container runtime. A standard container shares the Linux kernel of the host, so a kernel bug can allow code to escape. gVisor places a second, small kernel written in Go between the code and the host. This program is the **sentry**. It handles system calls itself and passes only a few safe requests to the host. A helper process, the **gofer**, handles file access.

`runsc` is the command-line tool that creates and runs these sandboxes. The Go worker drives it, and users do not call it directly.

The sandbox root filesystem is built for amd64 only. Running real sandboxes requires an amd64 Linux host with gVisor, so they do not run on Apple Silicon. Unit tests use fakes and run anywhere.

## Capabilities of a function

A function in a sandbox can do the following:

- Run Python as the root user of the sandbox. The process has user ID 0 but only a short list of Linux capabilities: audit write, kill, bind to low ports, and the file-ownership overrides needed to replace files in the shared base filesystem. It cannot gain new privileges.
- Use the pre-installed Python environment. The root filesystem already carries the packages that Readie targets.
- Install more packages. Names passed as `@remote(packages=[...])` are installed with `uv pip install` before the function runs. This requires outbound network access to the package index.
- Write files. The root filesystem is a copy-on-write overlay held in sandbox memory (setting `SANDBOX_OVERLAY`, default `root:memory`). Writes succeed, use memory, and disappear when the sandbox is destroyed. `/tmp` is a folder shared with the worker and is also removed with the container. `/dev/shm` is 1 GiB.
- Reach the internet, as described in [Network](#network).
- Use a GPU, on a GPU worker only. GPU workers turn on the `nvproxy` passthrough of gVisor. See [Flavors and catalogues](/docs/concepts/flavors-and-catalogues).

## Network

The worker setting `SANDBOX_NETWORK` accepts `none`, `sandbox`, or `host`. The default is `sandbox`. In this mode gVisor uses its own network stack inside the sandbox. The worker gives each sandbox a private network namespace that is joined to the host by a virtual network link, with network address translation (NAT) for outbound traffic.

Outbound connections to the internet work. Firewall rules that the worker installs block two destinations:

- the local network of the worker, which prevents code from reaching sibling services such as the router
- the link-local range `169.254.0.0/16`, where cloud providers serve instance metadata

If the worker cannot determine its own subnet, it fails to start instead of running with unrestricted egress. The network mode is part of the checkpoint fingerprint, so the worker and the pipeline must agree on it. Both default to `sandbox`.

## Resource limits

| Limit | Value | Source |
| --- | --- | --- |
| Memory | The `memory` set on `@remote`; otherwise the router default of 1 GiB (`DEFAULT_MEMORY`). | The router sets it per call, and the worker applies it as the container limit. |
| Memory growth | The worker raises the limit by a factor of 2 when use passes 90 percent, up to `max_memory` or the capacity share of the worker. | Worker settings `MEM_GROWTH_THRESHOLD` and `MEM_GROWTH_FACTOR`. |
| CPU | Half a core (quota 50000 per 100000 microsecond period). | Fixed in the worker. |
| Processes | 100 | Fixed in the worker. |
| Open files | 4096 | Fixed in the sandbox spec. |
| Run time | 1 hour per call by default. | Worker `EXECUTION_TIMEOUT` and router `EXECUTION_TIMEOUT`. |

If a call dies in a way that indicates an out-of-memory condition, the worker retries once in a new container with a larger limit. The retry applies only to a fresh container, never to the warm container of a session, and only if there is room to grow.

The CPU, process, and memory limits are written into the sandbox spec as cgroup limits. If the worker runs with `SANDBOX_IGNORE_CGROUPS=true`, gVisor does not enforce those cgroup limits. The `docker-compose.yml` in the repository defaults this setting to true, because Docker Desktop does not allow the required cgroup writes.

## Lifetime

A sandbox is created for a call. For a call without a [session](/docs/concepts/sessions), the sandbox is destroyed when the call ends. For a call in a session, a successful call leaves the sandbox running and idle so that the next call can reuse it. An idle sandbox is destroyed after `SANDBOX_IDLE_TTL` (default 5 minutes).

## What's next

- [Container lifecycle](/docs/architecture/container-lifecycle)
- [Security model](/docs/architecture/security-model)
- [Checkpoints](/docs/concepts/checkpoints)
