#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
: "${READIE_E2E_ENDPOINT:?Set the deployed TLS endpoint as host:port}"
: "${READIE_AUTH_TOKEN:?Set the deployed endpoint token}"
reports=${READIE_E2E_REPORTS:-$PWD/.build/e2e}
mkdir -p "$reports"
python=${PYTHON:-python3.12}
sdk_version=$("$python" -c 'from importlib.metadata import version; print(version("readie"))')
printf 'Endpoint: %s\nSDK version: %s\n' "$READIE_E2E_ENDPOINT" "$sdk_version" \
    | tee "$reports/deployment.txt"
"$python" -m pytest -v --junitxml="$reports/pytest.xml" e2e/test_system.py
