# Security

This is a research platform for running untrusted Python on shared
infrastructure. Two of its trust boundaries are inherent to that job rather than
defects, and are documented here so nobody mistakes them for oversights — and so
nobody mistakes the boundaries that *are* defects for design.

## Reporting a vulnerability

Open a private security advisory through GitHub's **Security → Report a
vulnerability** on this repository. Please do not open a public issue for
anything exploitable.

Include what you did, what happened, and which component. A proof of concept
helps; a working exploit is not required.

## The trust model

```
user's process          router            worker              sandbox
┌────────────┐        ┌────────┐        ┌────────┐        ┌────────────┐
│ readie-client│──gRPC─▶│ router │──gRPC─▶│ worker │──uds──▶│  executor  │
│            │◀───────│        │◀───────│        │◀───────│ user code  │
└────────────┘        └────────┘        └────────┘        └────────────┘
      ▲                                                          │
      └──────────── boundary 1 ──────────────────────────────────┘
                                          └─── boundary 2 ───────┘
```

### Boundary 1 — the client unpickles what the cluster sends it

`readie.CloudpickleCodec.decode_result` calls `cloudpickle.loads` on bytes that
arrived over the wire. Unpickling executes arbitrary code by design. A
compromised router or worker can therefore run code **in the user's own
process**, with that user's filesystem and credentials.

This cannot be fixed by validating the payload — it is what shipping live Python
objects means. It is confined rather than hidden: `ResultCodec`
(`pkg/src/readie/codec.py`) is a `Protocol`, so a deployment that cannot accept
this boundary can supply a codec restricted to a safe format and lose only the
ability to return arbitrary objects.

**Only point a client at a router you trust as much as you trust your own
laptop.**

### Boundary 2 — the sandbox runs arbitrary user code

That is the product. Containment is gVisor: the executor runs under `runsc`,
which intercepts syscalls in userspace rather than passing them to the host
kernel. Layered on top:

- **No network.** `SANDBOX_NETWORK=none` by default, and the worker validates it.
- **A read-only shared rootfs** with a per-sandbox copy-on-write overlay, so one
  execution cannot alter what the next one starts from. The worker rejects an
  `all:` overlay (it would hide the executor's socket) and any `:self` overlay
  (it would write into the tree every sandbox shares).
- **CPU, memory and PID limits** per container, from the router's estimate.
- **One socket, one interpreter, one execution at a time**, so two users' code is
  never co-resident in one address space.

The residual risk is a gVisor escape. `runsc` is pinned by release in one place,
`pipeline/Dockerfile`'s `runsc` stage: the capture tool copies it from there, and
the worker inherits it through `readie-worker-base`. Floating that pin would also
silently invalidate every checkpoint, since the save format is not stable across
releases. Track
[gVisor's advisories](https://github.com/google/gvisor/security/advisories) and
bump it together with regenerating the checkpoints — which also means a worker
rebuilt on a *new* base picks up a patched runsc, while `make worker-image` alone
does not.

## Deployment expectations

By default the router and worker speak **plaintext gRPC with no authentication** —
opt-in security keeps local runs, tests and the compose healthchecks working
without certs. Configure both before exposing the router:

- **Authorization (bearer token).** Set the router's `AUTH_TOKEN` and give the
  same token to clients (`READIE_AUTH_TOKEN`). The router
  then requires it on **`ProxyService`** — the only path that runs code — so
  reaching the port is no longer enough to execute on the cluster. The check is a
  server interceptor over a pluggable `Authenticator` (`grpcserver/auth.py`), so a
  deployment can swap the shared token for JWT or per-tenant validation. Health,
  reflection and `RegistryService` (workers) stay exempt.
- **Transport (TLS).** Handled by NGINX (see `nginx/README.md`).

What is **not** secured, by design (the "external only" boundary): the
router→worker call (`ExecutionService`) and the worker's own server stay
plaintext with no auth. Run the router↔worker mesh on a private network, and do
not expose the worker's port. Do not expose the router's port to anything you
would not hand a shell to.

## Non-vulnerabilities

- **A remote function reading its own container's filesystem.** It is a sandbox
  with a shared read-only base and a private overlay; that is the intended
  surface.
- **Resource exhaustion by a submitted function.** Bounded by the container
  limits and the execution deadline, both configurable. Denial of service by an
  authenticated user is an operational concern, not a vulnerability.
- **Reading `pipeline/data/*.json`.** Generated corpus data, not secrets.

## Secrets

`pipeline` reads `AZURE_API_KEY` and `AZURE_ENDPOINT` for corpus generation. They
are read from the environment or a `.env` file, both git-ignored, and are never
written to the corpus, the manifests, or a log line. Corpus generation is an
offline authoring step and is not part of the serving path.
