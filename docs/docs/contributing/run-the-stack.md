---
title: Run the stack locally
sidebar_position: 7
description: Start the NGINX proxy, router, and worker on one machine with Docker Compose and verify that they are healthy.
---

Use this procedure to start a full Readie stack on one machine: an NGINX proxy, the router, and one worker. The stack runs under Docker Compose, and `make run-local` wraps the commands.

:::warning Worker requires a gVisor host
The worker runs sandboxes with gVisor (`runsc`). gVisor works only on an amd64 Linux host and fails under emulation, so the worker does not run on Apple Silicon. The router and NGINX run anywhere Docker runs.

On a host without gVisor, the test suites still run, because they use fakes in place of gVisor. See [Run the tests without gVisor](#run-the-tests-without-gvisor).
:::

## Containers

Compose defines the following containers. The [glossary](/docs/guides/glossary) defines the terms router, worker, and sandbox.

| Container | Role | Port inside the network | Published on the host |
| --- | --- | --- | --- |
| `nginx` | Client-facing proxy (the `nginx:alpine` image). | `50051` (local config) | `50051` |
| `router` | Places calls on workers. | `50051` | none |
| `worker` | Restores checkpoints and runs functions in sandboxes. | `50052` | none |
| `worker-gpu` | Optional GPU worker (profile `gpu`). | `50052` | none |

Only NGINX is reachable from outside Compose. Clients connect to NGINX, NGINX forwards to the router, and the router connects to the workers. See the [architecture overview](/docs/architecture/overview).

## Prerequisites

- Docker with the Compose plugin.
- An amd64 Linux host that can run privileged containers. The worker runs with `privileged: true` and relaxed AppArmor and seccomp profiles, because gVisor needs both.
- The worker base image, `readie-worker-base-cpu:latest`. The image holds the gVisor binary, the root filesystem, and the checkpoints. The worker image is built `FROM` it, and unlike the router image it cannot be built from a bare checkout.

To build the base image, run `make worker-base`. Alternatively, run `make generation`, which captures checkpoints, builds the base image, and builds the worker image. See [Build checkpoints](/docs/contributing/build-checkpoints).

Without captured checkpoints, the worker starts but every request is a cold start. See [cold starts and restore](/docs/concepts/cold-starts-and-restore).

## Compose variables

Compose reads a `.env` file in the repository root. Copy `.env.example` to `.env` to change the values. These are the only variables the compose file substitutes.

| Variable | Default | Effect |
| --- | --- | --- |
| `READIE_IGNORE_CGROUPS` | `true` | Sets the worker's `SANDBOX_IGNORE_CGROUPS`. Required on Docker Desktop, where cgroup delegation blocks gVisor. Harmless on other platforms. |
| `READIE_WORKER_MEM_TOTAL` | `4Gi` | Memory the worker offers to the router. |
| `READIE_WORKER_MAX_EXECUTORS` | `8` | Maximum number of concurrent sandboxes. `0` means unlimited. |

The GPU worker also reads `READIE_WORKER_GPU_TOTAL` (default `16Gi`). Its memory and sandbox defaults differ from the CPU worker (`8Gi` and `4`). The GPU worker needs the NVIDIA container toolkit and a GPU image built with `make generation FLAVOR=gpu`. Start it with `docker compose --profile gpu up`.

## Steps

1. From the repository root, start the stack.

   ```bash
   make run-local
   ```

   The target runs the following command:

   ```bash
   docker compose \
     -f docker-compose.yml \
     -f docker-compose.local.yml \
     up -d --build
   ```

   The two compose files are layered:

   | File | Contribution |
   | --- | --- |
   | `docker-compose.yml` | The services: router, worker, optional GPU worker, and NGINX. |
   | `docker-compose.local.yml` | Publishes port `50051`, mounts `nginx/nginx.local.conf` (plaintext HTTP/2 gRPC), and sets the worker's `LOG_LEVEL` to `debug`. |

2. Confirm that the containers are running.

   ```bash
   docker compose ps
   ```

For a production-style stack with TLS, see [Deploy with TLS](/docs/contributing/deploy-with-tls). The `make run-prod` target layers `docker-compose.prod.yml` instead of the local file, publishes port `443`, and mounts a TLS configuration and certificates.

## Verify

The router and the worker each have a health check. Both run `grpcurl -plaintext <host>:<port> grpc.health.v1.Health/Check` every 10 seconds, with a 5 second timeout, 3 retries, and a 5 second start period. NGINX waits for the router to be healthy, and the worker also waits for the router. NGINX has no health check of its own.

To run the same checks manually:

```bash
docker compose exec router grpcurl -plaintext router:50051 grpc.health.v1.Health/Check
docker compose exec worker grpcurl -plaintext worker:50052 grpc.health.v1.Health/Check
```

A healthy service answers with the following status:

```text
"status": "SERVING"
```

To read the logs:

```bash
docker compose logs -f router
docker compose logs -f worker
docker compose logs nginx
```

The router and the worker log JSON by default. The following lines indicate a working stack:

- Worker: `registered with the router`, then `worker ready`. Together they mean the worker is available for placement.
- Router: `execution placed`, which includes the chosen worker, `checkpoint_id`, and `container_id`, then `execution finished`.

## Troubleshoot

### Worker reports serving but cannot run calls

A worker that has started but cannot serve, for example because it has no root filesystem, still reports as serving. The worker log contains the following line:

```text
worker serving but unusable; every execution will be refused
```

The error logged next to this line states the cause. Build the missing artifacts as described in [Build checkpoints](/docs/contributing/build-checkpoints).

### Restores or cold starts

To determine whether a call restored from a checkpoint or cold-started, see [Troubleshoot the stack](/docs/contributing/troubleshoot-the-stack#restore-or-cold-start).

## Stop the stack

To stop the stack, run:

```bash
make shutdown
```

The target runs `docker compose down --remove-orphans`. The router and the worker have a 30 second grace period to finish in-flight calls and release their sandboxes before Docker kills them.

## Run the tests without gVisor

Working on the code does not require a gVisor host, because most tests use fakes.

```bash
make install
make lint type test
```

The following targets and components require a real amd64 gVisor host:

- `make worker-base`, `make capture`, and `make generation`, which run gVisor.
- The worker's end-to-end test target.
- The running `worker` container.

See [Make targets](/docs/contributing/make-targets) for the full list.

## What's next

- [Configure the services](/docs/contributing/configure-services)
- [Troubleshoot the stack](/docs/contributing/troubleshoot-the-stack)
