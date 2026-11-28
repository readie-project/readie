ARG RUNSC_IMAGE
FROM ${RUNSC_IMAGE} AS runsc

FROM python:3.12-slim-bookworm AS rootfs
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv export --frozen --no-dev --no-emit-project --format requirements.txt -o /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    && rm -rf /build /tmp/requirements.txt /usr/local/bin/uv

# Cold starts still need the executor command from the manifest. There is
# no OCI checkpoint fingerprint to record because this image has no snapshots.
FROM rootfs AS manifest
RUN --mount=from=runsc,source=/usr/local/bin/runsc,target=/tmp/runsc \
    python - > /tmp/manifest.json <<'PYTHON'
import json
import subprocess

from readie_executor.protocol import PROTOCOL_VERSION

version = subprocess.check_output(["/tmp/runsc", "--version"], text=True)
print(json.dumps({
    "runsc_version": version.splitlines()[0],
    "spec_fingerprint": "cold-start-only",
    "executor_protocol": PROTOCOL_VERSION,
    "executor_argv": ["python", "-u", "-m", "readie_executor"],
}))
PYTHON

FROM debian:bookworm-slim
COPY --from=runsc /usr/local/bin/runsc /usr/local/bin/runsc
COPY --from=rootfs / /var/lib/readie/rootfs
COPY --from=manifest /tmp/manifest.json /var/lib/readie/manifest.json
RUN mkdir -p /shared /run/readie-runsc /var/log/runsc
