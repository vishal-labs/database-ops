#!/bin/bash
# One-time: creates the 'spider' database on the EXISTING postgres-container
# (same Postgres server the dashboard uses for 'vishal' -- this just adds a
# second, isolated database on it, it does not touch 'vishal').
set -euo pipefail
PG_NAME="${PG_CONTAINER:-postgres-container}"

if docker exec "$PG_NAME" psql -U vishal -lqt | cut -d'|' -f1 | grep -qw spider; then
  echo "'spider' database already exists on $PG_NAME"
else
  echo "creating 'spider' database on $PG_NAME"
  docker exec "$PG_NAME" createdb -U vishal spider
fi
