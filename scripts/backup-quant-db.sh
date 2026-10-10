#!/usr/bin/env bash
# Consistent full recovery bundle: databases, state, code, configuration and credentials.
set -euo pipefail
REFACTOR_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REFACTOR_ROOT"
exec "$REFACTOR_ROOT/.venv/bin/python" -m qsim.services.recovery backup
