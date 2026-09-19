#!/bin/bash
# Brings up the full stack: postgres container -> postgres-mcp container -> opencode serve -> dashboard server
# Safe to run repeatedly; logs land in ./logs (survives reboots, unlike /tmp).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
log() { echo "[$(date '+%H:%M:%S')] $*"; }

PG_NAME=postgres-container
MCP_NAME=postgres-mcp

# --- 1. postgres container (start if stopped, recreate if missing) ---
if docker ps -a --format '{{.Names}}' | grep -q "^${PG_NAME}$"; then
  docker ps --format '{{.Names}}' | grep -q "^${PG_NAME}$" || { log "starting $PG_NAME"; docker start "$PG_NAME"; }
else
  log "creating $PG_NAME"
  docker run -d --name "$PG_NAME" \
    -e POSTGRES_PASSWORD=password -e POSTGRES_USER=vishal -p 5432:5432 \
    postgres:17-alpine
  sleep 3
fi

# --- 2. wait for postgres to accept connections, seed if empty ---
for i in {1..30}; do
  docker exec "$PG_NAME" pg_isready -U vishal -d vishal >/dev/null 2>&1 && break
  [ "$i" -eq 30 ] && { log "postgres never became ready"; exit 1; }
  sleep 1
done
log "postgres is up and ready"

if ! docker exec "$PG_NAME" psql -U vishal -d vishal -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='orders'" 2>/dev/null | grep -q 1; then
  log "seeding database"
  docker exec -i "$PG_NAME" psql -U vishal -d vishal < seed.sql >/dev/null
fi

# --- 3. postgres-mcp container (SSE server on :8000) ---
# Always recreated: it is stateless, and a started-again container would keep a stale connection.
docker rm -f "$MCP_NAME" >/dev/null 2>&1 || true
log "creating $MCP_NAME"
docker run -d --name "$MCP_NAME" \
  -e DATABASE_URI="postgresql://vishal:password@host.docker.internal:5432/vishal" \
  -p 8000:8000 \
  crystaldba/postgres-mcp --access-mode=restricted --transport=sse

# Run a command in a new session so aborting the parent shell cannot kill it.
start_detached() {
  local log="$1"; shift
  python3 - "$log" "$@" <<'PYEOF' >/dev/null 2>&1
import subprocess, sys
with open(sys.argv[1], "a") as f:
    subprocess.Popen(sys.argv[2:], stdout=f, stderr=f, start_new_session=True)
PYEOF
}

# --- 4. opencode serve ---
if ! pgrep -f "opencode serve" >/dev/null; then
  log "starting opencode serve"
  start_detached logs/opencode-serve.log opencode serve --port 4096
  for i in {1..15}; do
    curl -sf http://localhost:4096/global/health >/dev/null && break
    [ "$i" -eq 15 ] && { log "opencode serve never became healthy"; exit 1; }
    sleep 1
  done
fi
log "opencode serve up"

# --- 5. dashboard server (always fresh) ---
pkill -f "node server.js" 2>/dev/null || true
sleep 1
# Host connection uses localhost since port 5432 is mapped
DATABASE_URL="postgresql://vishal:password@127.0.0.1:5432/vishal" \
  start_detached logs/dashboard-server.log node server.js

for i in {1..10}; do
  curl -sf http://localhost:3000/api/health >/dev/null && break
  [ "$i" -eq 10 ] && { log "dashboard server failed, see logs/dashboard-server.log"; exit 1; }
  sleep 1
done

log "everything up:"
log "  dashboard:  http://localhost:3000"
log "  opencode:   http://localhost:4096"
log "  logs:       ./logs/"