#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    echo "The real gVisor fixture requires Linux/amd64." >&2
    exit 1
fi

reports=${READIE_E2E_REPORTS:-$PWD/.build/e2e}
mkdir -p "$reports"
work=$(mktemp -d)
project="readie-e2e-$(openssl rand -hex 8)"
runsc_image="$project-runsc"
base_image="$project-base"
export E2E_WORKER_IMAGE="$project-worker"
export E2E_TLS_DIR="$work/tls"
compose=(docker compose --env-file /dev/null -p "$project" -f e2e/compose.yml)

cleanup() {
    status=$?
    trap - EXIT
    if (( status != 0 )); then
        "${compose[@]}" ps -a > "$reports/compose-status.txt" 2>&1 || true
        "${compose[@]}" logs --no-color > "$reports/compose.log" 2>&1 || true
        mkdir -p "$reports/runsc"
        "${compose[@]}" cp worker:/var/log/runsc/. "$reports/runsc/" || true
    fi
    if ! "${compose[@]}" down --volumes --remove-orphans --rmi local; then
        status=1
    fi
    docker image rm "$E2E_WORKER_IMAGE" "$base_image" "$runsc_image" >/dev/null 2>&1 || true
    rm -rf "$work"
    exit "$status"
}
export READIE_AUTH_TOKEN
READIE_AUTH_TOKEN=$(openssl rand -hex 32)
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir "$E2E_TLS_DIR"
(
    umask 077
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=Readie-E2E-CA \
        -keyout "$work/ca.key" -out "$work/ca.crt"
    openssl req -new -newkey rsa:2048 -nodes -subj /CN=localhost \
        -keyout "$E2E_TLS_DIR/server.key" -out "$work/server.csr"
    printf '%s\n' 'subjectAltName=DNS:localhost,IP:127.0.0.1' 'extendedKeyUsage=serverAuth' \
        > "$work/server.ext"
    openssl x509 -req -in "$work/server.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" \
        -CAcreateserial -days 1 -extfile "$work/server.ext" -out "$E2E_TLS_DIR/server.crt"
)
export READIE_TLS_CA="$work/ca.crt"

"${PYTHON:-python3.12}" -m venv "$work/venv"
python="$work/venv/bin/python"
"$python" -m pip wheel --no-deps --wheel-dir "$work/wheels" ./pkg
"$python" -m pip install -r e2e/requirements.txt "$work"/wheels/*.whl

# Build only the shared runsc stage. It needs no repository build context.
docker build --network="${READIE_E2E_BUILD_NETWORK:-default}" --target runsc -t "$runsc_image" - < pipeline/Dockerfile
docker build --network="${READIE_E2E_BUILD_NETWORK:-default}" -f e2e/worker-base.Dockerfile --build-arg "RUNSC_IMAGE=$runsc_image" \
    -t "$base_image" executor
docker build --network="${READIE_E2E_BUILD_NETWORK:-default}" --build-arg "BASE_IMAGE=$base_image" -t "$E2E_WORKER_IMAGE" worker
"${compose[@]}" build router
"${compose[@]}" up -d --wait --wait-timeout 90
export READIE_E2E_ENDPOINT
READIE_E2E_ENDPOINT=$("${compose[@]}" port nginx 443)
"$python" e2e/wait_for_worker.py
"$python" -m pytest -v --junitxml="$reports/pytest.xml" e2e/test_system.py
