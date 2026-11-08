#!/usr/bin/env bash
# Outcome-First Autonomous Controller entrypoint
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"

export PYTHONPATH="${PYTHONPATH:-.}:."
echo "Starting Outcome-First Autonomous Controller against $SIM_URL" >&2
exec python -m controller.main --url "$SIM_URL" --db controller.db --tick 60
