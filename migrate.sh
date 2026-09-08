#!/bin/bash
# Applies pending migrations in order, tracked in schema_migrations. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
PG_NAME="${PG_CONTAINER:-postgres-container}"
PG_ARGS=(-U vishal -d vishal)

mkdir -p migrations
container exec "$PG_NAME" psql "${PG_ARGS[@]}" -q -c \
  "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT now())" >/dev/null

shopt -s nullglob
applied_any=0
for f in migrations/*.sql; do
  base=$(basename "$f")
  if container exec "$PG_NAME" psql "${PG_ARGS[@]}" -tAc \
       "SELECT 1 FROM schema_migrations WHERE name = '$base'" | grep -q 1; then
    echo "skip    $base (already applied)"
    continue
  fi
  echo "apply   $base"
  container exec -i "$PG_NAME" psql "${PG_ARGS[@]}" -v ON_ERROR_STOP=1 -q < "$f"
  container exec "$PG_NAME" psql "${PG_ARGS[@]}" -q -c \
    "INSERT INTO schema_migrations (name) VALUES ('$base')" >/dev/null
  echo "done    $base"
  applied_any=1
done
[ "$applied_any" -eq 0 ] && echo "no pending migrations"
exit 0
