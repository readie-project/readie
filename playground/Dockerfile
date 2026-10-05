# Build from the repo root (the readie client is a path dependency):
#   docker build -f playground/Dockerfile -t readie-playground:latest .
FROM python:3.12-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /usr/local/bin/uv
WORKDIR /app

# The client is installed from source, at the same relative path pyproject uses.
COPY pkg ./pkg
COPY playground/pyproject.toml playground/uv.lock playground/README.md ./playground/
WORKDIR /app/playground
RUN uv sync --frozen --no-install-project --no-dev
COPY playground/src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /app /app
ENV PATH="/app/playground/.venv/bin:$PATH"

# Unprivileged, and the only outbound traffic needed is to the Readie router.
RUN useradd --system --uid 10001 --no-create-home playground
USER playground
EXPOSE 8080
CMD ["python", "-m", "readie_playground"]
