#!/usr/bin/env bash
# Mirror the CI checks defined in .github/workflows/release.yml so that
# local results match what GitHub Actions will run. Run this before every
# push to catch drift early.
set -e

echo "=== Lint (ruff check) ==="
ruff check .

echo "=== Lint (ruff format --check) ==="
ruff format --check .

echo "=== Tests (pytest -q -n auto) ==="
pytest -q -n auto

echo "=== All CI checks passed ==="
