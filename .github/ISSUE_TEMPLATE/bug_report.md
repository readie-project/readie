---
name: Bug report
about: Report behavior that differs from the documentation
labels: bug
---

## Component

<!-- router / worker / executor / pkg (client SDK) / pipeline / infrastructure -->

## What happened

<!-- Include the exact error and the structured log lines around it. The
     router and worker log JSON with request_id and container_id. These two
     fields let a report be traced across components. -->

## What you expected

## Reproduction

<!-- The smallest example that shows the problem. If it involves a remote
     function, include the function body: what it imports and what it returns. -->

## Environment

- Host OS and architecture:
- Component version or commit:
- Running via `docker compose`, or from source:

<!-- If this involves executing a function rather than only the router/worker
     gRPC path: gVisor is amd64-only, so this cannot reproduce on Apple Silicon.
     State which platform you are on. -->
