---
title: Glossary
sidebar_position: 8
description: Definitions of the terms used across the Readie documentation, in alphabetical order.
---

Each entry defines one term and links to the page that describes it in depth.

## A

### alpha

A number, in seconds per megabyte, that sets how much the size of a checkpoint counts against the import time that it saves. The pipeline uses it when planning checkpoints and the router uses it when choosing one, so both must use the same value. See [Cost model](/docs/concepts/cost-model).

### auto-expand

A worker behavior that raises the memory limit of a container when usage approaches the limit. Growth stops at the `max_memory` set on `@remote`, or at the capacity of the worker if none is set. See [Packages and resources](/docs/getting-started/packages-and-resources).

## C

### catalogue

A JSON file, one per flavor, that lists every checkpoint the pipeline built and the packages that each contains. The router loads catalogues at startup to choose a checkpoint for each call. If none exist, every start is a cold start. See [Flavors and catalogues](/docs/concepts/flavors-and-catalogues).

### checkpoint

A saved copy of a running Python process, taken after the process has imported a set of packages. Restoring a checkpoint is faster than starting Python and importing those packages again. See [Checkpoints](/docs/concepts/checkpoints).

### cloudpickle

A Python library that serializes functions, including functions defined in a script. The client uses it to turn the function and its arguments into bytes, and the executor turns the bytes back into a function. Both sides must run the same Python minor version. See the [FAQ](/docs/getting-started/faq).

### cold start

A delay that occurs when a function runs on a fresh interpreter that must import its libraries first. Readie shortens this delay. See [Cold starts and restore](/docs/concepts/cold-starts-and-restore).

### container

A running sandbox, as tracked by the worker. Logs and settings use "container" where this documentation uses "sandbox". See [Sandboxes](/docs/concepts/sandboxes) and [Container lifecycle](/docs/architecture/container-lifecycle).

### corpus

A collection of example requests, consisting of Python snippets and the imports they use, that the pipeline analyzes to decide which packages belong in checkpoints. The pipeline generates it offline. See [Building checkpoints](/docs/architecture/building-checkpoints).

## D

### dependency closure

A set of packages that contains a package, everything it imports, and everything those packages import in turn. The router prices a checkpoint by the closure of what a request needs, not only by the names that the request lists. See [Cost model](/docs/concepts/cost-model).

## E

### executor

A Python program that runs inside every sandbox. It receives the pickled function over a unix socket, calls it, and returns the result. See [Components](/docs/concepts/components) and [Executor protocol](/docs/architecture/executor-protocol).

## F

### flavor

A hardware kind, `cpu` or `gpu`, that a worker and its checkpoints are built for. A GPU request goes only to GPU workers. See [Flavors and catalogues](/docs/concepts/flavors-and-catalogues).

## G

### generation

A complete set of artifacts for one flavor: the captured checkpoints, the manifest, and the catalogue. The command `make generation` builds one and bakes it into a worker image. See [Build checkpoints](/docs/contributing/build-checkpoints).

### gRPC

A remote procedure call framework that uses messages defined in protobuf files. Every Readie component except the executor communicates with its neighbors through gRPC. See [Protos reference](/docs/architecture/protos).

### gVisor

A sandbox technology from Google that intercepts the system calls of a program in user space instead of passing them to the host kernel. Readie uses it to isolate user code and to take checkpoints. It runs on Linux amd64 only. See [Sandboxes](/docs/concepts/sandboxes).

## I

### idle TTL

A duration that a warm container may remain unused before the worker destroys it. The setting is `SANDBOX_IDLE_TTL` and the default is 5 minutes. The check runs every 30 seconds. See [Container lifecycle](/docs/architecture/container-lifecycle).

## M

### manifest

A `manifest.json` file written when checkpoints are captured. It records, among other values, the executor command and the executor protocol version. The worker reads it and refuses artifacts that it cannot work with. See [Building checkpoints](/docs/architecture/building-checkpoints).

## O

### OCI spec

A `config.json` file that describes a sandbox to the runtime, including its program, mounts, and settings. The pipeline and the worker generate it with the same Go code, because a checkpoint restores only into a sandbox of the same shape. See [Sandboxes](/docs/concepts/sandboxes).

## P

### placement

A routing decision that selects the worker for a call. The router considers capacity, flavor, session affinity, and which checkpoint saves the most time. See [Placement](/docs/architecture/placement).

### planner

A pipeline component that decides which packages go into which checkpoints. The default is a greedy planner that keeps adding a package while the time it saves is worth more than its size cost. A fixed planner uses a set of packages that the user chooses. See [Building checkpoints](/docs/architecture/building-checkpoints).

### protobuf

Protocol Buffers, a format for describing structured messages and services in `.proto` files. Code generators turn those files into Python and Go code. See [Changing protos](/docs/contributing/changing-protos).

## R

### registry

A live list of workers and their status that the router maintains. Workers report to it through the `RegistryService`. It is held in memory, so a router restart empties it until workers report again. See [Components](/docs/concepts/components).

### restore

An operation that brings a checkpoint back to life as a running process in a new sandbox. See [Cold starts and restore](/docs/concepts/cold-starts-and-restore).

### rootfs

A root filesystem that every sandbox sees, consisting of the operating system and the installed Python packages. It is read-only and shared, and each sandbox gets its own writable layer on top. A checkpoint restores only into the rootfs it was captured from. See [Sandboxes](/docs/concepts/sandboxes).

### router

A service that receives calls from clients and decides which worker runs them. It also maintains the registry. See [Components](/docs/concepts/components).

### runsc

A command-line program that runs gVisor sandboxes. The worker calls it to create, checkpoint, and restore sandboxes. See [Sandboxes](/docs/concepts/sandboxes).

## S

### sandbox

An isolated environment where a function runs, separate from the host machine and from the code of other users. In Readie, a sandbox is a gVisor container. See [Sandboxes](/docs/concepts/sandboxes).

### session

A handle that pins a series of calls to the same warm container. State that one call leaves in memory, such as a loaded model, is available to the next call. Calls in a session run one at a time. See [Sessions](/docs/concepts/sessions).

### spec fingerprint

A hash of the parts of a sandbox that a checkpoint depends on: the program, the mount list, the namespaces, and the runtime modes. The worker checks it before a restore, so a checkpoint is never restored into a sandbox of a different shape. See [Sandboxes](/docs/concepts/sandboxes).

## W

### worker

A Go service that starts sandboxes, restores checkpoints, communicates with the executor, and reports its load to the router. See [Components](/docs/concepts/components).

## Symbols

### `@remote`

A decorator from the `readie` package that marks a function to run on a Readie cluster instead of locally. It accepts options for memory, GPU, packages, and more. See the [SDK reference](/docs/guides/sdk) and the [Quickstart](/docs/getting-started/quickstart).
