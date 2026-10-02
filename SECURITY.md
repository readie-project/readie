# Security

Readie is a research platform for running untrusted Python on shared
infrastructure. Two of its trust boundaries are inherent to that purpose and are
not defects. They are documented here so that they are not mistaken for
oversights, and so that the boundaries that are defects are not mistaken for
design. The documentation site describes the same model in
[Security model](https://readie.org/docs/architecture/security-model) and
[Security](https://readie.org/docs/architecture/security).

## Reporting a vulnerability

Report a vulnerability privately through a GitHub security advisory on this
repository: select **Security**, then **Report a vulnerability**. Do not open a
public issue for anything exploitable.

Include what you did, what happened, and which component is affected. A proof of
concept helps, but a working exploit is not required.

## The trust model

```text
user's process            router            worker              sandbox
┌──────────────┐        ┌────────┐        ┌────────┐        ┌────────────┐
│readie-client │──gRPC─▶│ router │──gRPC─▶│ worker │──uds──▶│  executor  │
│              │◀───────│        │◀───────│        │◀───────│ user code  │
└──────────────┘        └────────┘        └────────┘        └────────────┘
       ▲                                                           │
       └─────────────────────── boundary 1 ────────────────────────┘
                                                └── boundary 2 ────┘
```

### Boundary 1: the client unpickles what the cluster sends

`readie.CloudpickleCodec.decode_result` calls `cloudpickle.loads` on bytes that
arrived over the wire. Unpickling executes arbitrary code by design. A
compromised router or worker can therefore run code in the user's own process,
with that user's filesystem and credentials.

Validating the payload cannot prevent this, because returning live Python objects
is the feature. The risk is confined to one place. `ResultCodec`
(`pkg/src/readie/codec.py`) is a `Protocol`, so a deployment that cannot accept
this boundary can supply a codec restricted to a safe format. The cost is that the
client can no longer return arbitrary objects.

Point a client only at a router that you trust as much as the machine the client
runs on.

### Boundary 2: the sandbox runs arbitrary user code

Running arbitrary user code is the product. Containment is gVisor: the executor
runs under `runsc`, which handles syscalls in userspace and does not pass them to
the host kernel. The worker adds the following controls:

- Network access is on by default, for `@remote(packages=...)`. The worker passes
  `SANDBOX_NETWORK` straight through to `runsc --network` and defaults to
  `sandbox` when the variable is unset. A deployment therefore gets real, routed
  network access by default, because installing packages at request time (`uv pip
  install` reaching PyPI) is the common case this project is built around. Set
  `SANDBOX_NETWORK=none` explicitly for the strict, network-isolated posture;
  `none` is a deliberate opt-out and not the default. In every mode, packages
  installed at request time carry the supply-chain risk of an unpinned or
  mistyped package name. When network access is needed, prefer `sandbox` over
  `host`: `host` shares the worker's own network namespace with sandboxed code,
  whereas `sandbox` keeps gVisor's own netstack isolation. Setting
  `SANDBOX_NETWORK=sandbox` is what makes the worker provision a real network
  namespace for the sandbox to join (see "Sandbox networking" in
  [`worker/README.md`](worker/README.md)). There is no separate opt-in, so
  choosing `sandbox` means real, routed network access and not a loopback-only
  network. Provisioning excludes the worker's own Docker-network subnet and
  link-local addresses (`169.254.0.0/16`, where cloud metadata endpoints live)
  from a sandbox's egress. The network access exists to reach PyPI, and it is not
  a path back to the router (unauthenticated by default, see below) or to sibling
  containers.
- A read-only shared rootfs with a per-sandbox copy-on-write overlay, so that one
  execution cannot alter what the next one starts from. The worker rejects an
  `all:` overlay, because it would hide the executor's socket, and any `:self`
  overlay, because it would write into the tree that every sandbox shares.
- CPU, memory, and PID limits per container. The memory limit comes from the
  request's resource budget. The CPU limit (half a core) and the PID limit (100)
  are fixed values in the worker.
- One socket, one interpreter, and one execution at a time, so that the code of
  two users is never co-resident in one address space.

The residual risk is a gVisor escape. `runsc` is pinned by release in one place,
the `runsc` stage of `pipeline/Dockerfile`. The capture tool copies it from there,
and the worker inherits it through `readie-worker-base`. Floating that pin would
also silently invalidate every checkpoint, because the save format is not stable
across releases. Track
[gVisor's advisories](https://github.com/google/gvisor/security/advisories) and
update the pin together with regenerating the checkpoints. A worker rebuilt on a
new base picks up a patched `runsc`, whereas `make worker-image` alone does not.

## Deployment expectations

By default, the router and worker speak plaintext gRPC with no authentication.
Security is opt-in so that local runs, tests, and the compose healthchecks work
without certificates. Configure both controls before exposing the router:

- Authorization (bearer token). Set the router's `AUTH_TOKEN` and give the same
  token to clients as `READIE_AUTH_TOKEN`. The router then requires the token on
  `ProxyService`, the only path that runs code, so reaching the port is no longer
  enough to execute code on the cluster. The check is a server interceptor over a
  pluggable `Authenticator` (`grpcserver/auth.py`), so a deployment can replace the
  shared token with JWT or per-tenant validation. Health checks, reflection, and
  `RegistryService` (workers) are exempt.
- Transport (TLS). NGINX terminates TLS. See [`nginx/README.md`](nginx/README.md).

The router-to-worker call (`ExecutionService`) and the worker's own server are not
secured, by design (the "external only" boundary). They remain plaintext with no
authentication. Run the router-to-worker mesh on a private network, and do not
expose the worker's port. Do not expose the router's port to anyone you would not
give a shell to.

## Non-vulnerabilities

- A remote function reading its own container's filesystem. The sandbox has a
  shared read-only base and a private overlay, and this is the intended surface.
- Resource exhaustion by a submitted function. The container limits and the
  execution deadline bound it, and both are configurable. Denial of service by an
  authenticated user is an operational concern and not a vulnerability.
- Reading `pipeline/data/**/*.json`. This is generated corpus data and contains no
  secrets.

## Secrets

`pipeline` reads `AZURE_API_KEY` and `AZURE_ENDPOINT` for corpus generation. It
reads them from the environment or from a `.env` file, both of which are
git-ignored, and never writes them to the corpus, the manifests, or a log line.
Corpus generation is an offline authoring step and is not part of the serving
path.
