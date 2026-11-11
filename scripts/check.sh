#!/usr/bin/env sh
# Full local check (Linux/macOS). Same steps as CI. Stops at the first failure.
set -eu
cd "$(dirname "$0")/.."

for step in "ruff format --check ." "ruff check ." "mypy" "lint-imports" "pytest"; do
  echo "==> uv run $step"
  # shellcheck disable=SC2086
  uv run $step
done
echo "All checks passed."
