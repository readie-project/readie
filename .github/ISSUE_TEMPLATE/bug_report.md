---
name: Bug report
about: Something behaves differently than documented
labels: bug
---

## Component

<!-- router / worker / executor / pkg (client SDK) / pipeline / infrastructure -->

## What happened

<!-- Include the exact error, and the structured log lines around it. The
     router and worker log JSON with request_id and container_id — those two
     fields are what let a report be traced across components. -->

## What you expected

## Reproduction

<!-- The smallest thing that shows it. If it involves a remote function, the
     function body matters: what it imports and what it returns. -->

## Environment

- Host OS and architecture:
- Component version or commit:
- Running via `docker compose`, or from source:

<!-- If this involves executing a function rather than only the router/worker
     gRPC path: gVisor is amd64-only, so this cannot reproduce on Apple Silicon.
     Please say which you are on. -->
