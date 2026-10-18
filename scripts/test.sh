#!/usr/bin/env bash
# Unit tests. Used by CI and NexKit.
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/pytest -q
