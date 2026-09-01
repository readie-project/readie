# Slim rather than alpine: pydantic-core and grpcio publish manylinux wheels but
# not musl ones, so alpine would build both from source on every image build.
FROM python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# grpcurl backs the compose healthcheck. The archive is architecture-specific,
# so derive it rather than pinning x86_64 as the previous Dockerfile did.
FROM debian:bookworm-slim AS tools
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) GRPCURL_ARCH=x86_64 ;; \
        arm64) GRPCURL_ARCH=arm64  ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && wget -qO- "https://github.com/fullstorydev/grpcurl/releases/download/v1.9.3/grpcurl_1.9.3_linux_${GRPCURL_ARCH}.tar.gz" \
       | tar -xz -C /usr/local/bin grpcurl

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies first, from the lockfile, so this layer survives source edits.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

FROM base
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=tools /usr/local/bin/grpcurl /usr/local/bin/grpcurl
ENV PATH="/app/.venv/bin:$PATH"

# Run unprivileged: the router talks to workers over the network and needs
# nothing from the host.
RUN useradd --system --uid 10001 --no-create-home router
USER router

ENV SERVICE_NAME=router \
    PORT=50051 \
    BIND_HOST=0.0.0.0 \
    APP_ENV=production \
    LOG_LEVEL=info \
    LOG_FORMAT=json

EXPOSE 50051

CMD ["python", "-m", "readie_router"]
