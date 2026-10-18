#!/usr/bin/env bash
# Lint and format check. Used by CI and NexKit.
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/ruff check .
.venv/bin/ruff format --check .
