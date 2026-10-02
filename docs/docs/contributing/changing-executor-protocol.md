---
title: Change the executor protocol
sidebar_position: 4
description: How the worker and executor frame bytes over a socket, and the files to update together when that format changes.
---

The worker (Go) and the [executor](/docs/concepts/glossary) (Python) communicate over a unix socket inside each sandbox. The framing format is implemented once in each language, so a change must land in both implementations at the same time. This page applies only to changes in how requests or results are framed on that socket. For gRPC messages, see [Change protos](/docs/contributing/changing-protos).

## Current format

A message is a series of chunks. Each chunk is an 8-byte big-endian length followed by that many bytes. A chunk of length zero ends the message.

```text
message:  ([8-byte length][chunk bytes])*  [8 zero bytes]
request:  one message holding the cloudpickle of {func, args, kwargs}
response: one message holding the cloudpickle of a result envelope
```

The result envelope is a small dictionary. On success it is `{"ok": True, "value": ...}`. When the function raises, it is `{"ok": False, "exc_type": ..., "message": ..., "traceback": ...}`. A raised exception is data and does not indicate a worker failure.

The current version number is 2. For the design, see [Executor protocol](/docs/architecture/executor-protocol).

## Code locations

| What | Where |
| --- | --- |
| Python framing and `PROTOCOL_VERSION` | `executor/src/readie_executor/protocol.py` |
| Go framing and `ProtocolVersion` | `worker/internal/executor/protocol.go` |
| Version written into the manifest | `pipeline/src/readie_pipeline/config.py` (`EXECUTOR_PROTOCOL`) |
| Version check in the worker | `worker/internal/artifact/artifact.go` |
| Shared test fixture | `executor/tests/data/frames.golden.json` |
| Script that writes the fixture | `executor/tests/generate_golden.py` |

The client SDK (`pkg/`) does not use the framing. It reads only the result envelope after the worker and router relay it. If the shape of the envelope changes, update `pkg/src/readie/protocol.py` as well. The golden fixture does not cover the envelope, so add tests for it.

## Golden fixture

Two hand-written copies of one format can drift apart. The fixture is a JSON file of example messages, with each payload and its exact encoded bytes in hex. The Python tests and the Go tests both read the fixture and check that they decode every case. If one side changes without the other, a test fails during development instead of a restore failing in deployment.

The generator script writes the encoded bytes by hand with `struct.pack` and does not call the framing code. This is deliberate, because a fixture produced by the code under test cannot detect a change to that code.

## Steps

1. Change the Python framing in `executor/src/readie_executor/protocol.py`.
2. Change the Go framing in `worker/internal/executor/protocol.go` to match.
3. If the change creates a new situation worth pinning, add cases to the `CASES` list in `executor/tests/generate_golden.py`.
4. If the new format cannot read the old one, bump the version. Change all of the following by hand:
   - `PROTOCOL_VERSION` in `executor/src/readie_executor/protocol.py`
   - `ProtocolVersion` in `worker/internal/executor/protocol.go`
   - `EXECUTOR_PROTOCOL` in `pipeline/src/readie_pipeline/config.py`
   - the `"protocol_version"` value inside `main()` in `executor/tests/generate_golden.py`
   - the test `test_the_version_is_two` in `executor/tests/unit/test_protocol.py`, which pins the number
5. Regenerate the fixture.

   ```bash
   cd executor
   uv run python tests/generate_golden.py
   ```

6. Run both test suites.

   ```bash
   make executor-test
   make worker-test
   ```

7. Commit the fixture together with the code changes.

## Verify

If the version was bumped but the fixture was not regenerated, the Python test `test_the_fixture_matches_this_protocol_version` and the Go test `TestGolden_MatchesThisProtocolVersion` both fail. Passing suites confirm that the fixture and both implementations agree.

## Effect of a version bump on deployments

The pipeline writes `executor_protocol` into the manifest of each generation when it captures checkpoints. When the worker loads a generation, it compares that number with its own. If the numbers differ, the worker refuses the generation, reports which two numbers disagree, and asks for a rebuild of the worker image with the current pipeline.

A bump therefore has a cost. Every checkpoint already captured contains the old executor. Run `make generation` again (see [Build checkpoints](/docs/contributing/build-checkpoints)) and redeploy. In the pull request, select "Executor protocol version bumped" and "Generation manifest changed". See [Pull requests](/docs/contributing/pull-requests).

## What CI checks

The `golden` job runs `uv run python tests/generate_golden.py` inside `executor/`, then fails if `git diff` shows any change under `executor/tests/data`. A stale fixture therefore fails the pull request. The worker `go test -race` job and the executor `pytest` job are the jobs that read the fixture.

## What's next

- [Executor protocol](/docs/architecture/executor-protocol)
- [Conventions](/docs/contributing/conventions)
