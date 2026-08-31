#!/usr/bin/env bash
# Run each stage against a freshly restarted Postgres.
#
# A long-lived server measures slower than a new one. Reps at 90% of capacity read
# 680-998ms after an hour of continuous load, and 67-173ms immediately after a restart,
# with the workers only reaching the offered rate in the restarted case. Restarting
# between stages takes that drift out of the comparison. It also measures a state no
# production deployment lives in, which is why it is a separate script and not the
# default: `postgres_uptime_s` in every result records which regime produced it.
#
# Usage: benchmarks/run_stages_cold.sh [stage ...]   (default: a b c d e f g)
# Stages must run in order on a fresh results directory: B, C and E calibrate from A,
# and G calibrates from B, each reading the earlier stage back off disk.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f benchmarks/compose.yaml)
RUN_STAGE="uv sync --frozen \
  && uv run python benchmarks/manage.py migrate \
  && uv run python -m benchmarks.stages"

stages=("$@")
if [ ${#stages[@]} -eq 0 ]; then
  stages=(a b c d e f g)
fi

for stage in "${stages[@]}"; do
  echo "=== restarting db_bench before stage ${stage} ==="
  "${COMPOSE[@]}" restart db_bench
  until "${COMPOSE[@]}" exec -T db_bench pg_isready -q -U postgres; do sleep 1; done
  "${COMPOSE[@]}" run --rm bench sh -c "${RUN_STAGE} --stage ${stage}"
done
