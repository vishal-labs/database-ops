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
if container ls -a --format json | grep -q "\"$PG_NAME\""; then
  container ls --format json | grep -q "\"$PG_NAME\"" || { log "starting $PG_NAME"; container start "$PG_NAME"; }
else
  log "creating $PG_NAME"
  container run -d --name "$PG_NAME" \
    -e POSTGRES_PASSWORD=password -e POSTGRES_USER=vishal -p 5432:5432 \
    postgres:17-alpine
  sleep 3
fi

# --- 2. wait for postgres to accept connections, seed if empty ---
PG_IP=$(container ls --format json | python3 -c "import json,sys; print([c['status']['networks'][0]['ipv4Address'].split('/')[0] for c in json.load(sys.stdin) if c['id']=='$PG_NAME'][0])")
for i in {1..30}; do
  container exec "$PG_NAME" pg_isready -U vishal -d vishal >/dev/null 2>&1 && break
  [ "$i" -eq 30 ] && { log "postgres never became ready"; exit 1; }
  sleep 1
done
log "postgres up at $PG_IP"
if ! container exec "$PG_NAME" psql -U vishal -d vishal -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='orders'" 2>/dev/null | grep -q 1; then
  log "seeding database"
  container exec -i "$PG_NAME" psql -U vishal -d vishal < seed.sql >/dev/null
fi

# --- 3. postgres-mcp container (SSE server on :8000) ---
# Always recreated: it is stateless, and a started-again container would keep a stale
# DATABASE_URI pointing at an old postgres IP.
container rm -f "$MCP_NAME" >/dev/null 2>&1 || true
log "creating $MCP_NAME"
container run -d --name "$MCP_NAME" \
  -e DATABASE_URI="postgresql://vishal:password@$PG_IP:5432/vishal" \
  -p 8000:8000 \
  crystaldba/postgres-mcp --access-mode=restricted --transport=sse

# --- 4. opencode serve ---
if ! pgrep -f "opencode serve" >/dev/null; then
  log "starting opencode serve"
  nohup opencode serve --port 4096 > logs/opencode-serve.log 2>&1 &
  for i in {1..15}; do
    curl -sf http://localhost:4096/global/health >/dev/null && break
    [ "$i" -eq 15 ] && { log "opencode serve never became healthy"; exit 1; }
    sleep 1
  done
fi
log "opencode serve up"

# --- 5. dashboard server (always fresh, so it picks up the current container IP) ---
pkill -f "node server.js" 2>/dev/null || true
sleep 1
DATABASE_URL="postgresql://vishal:password@$PG_IP:5432/vishal" \
  nohup node server.js > logs/dashboard-server.log 2>&1 &
for i in {1..10}; do
  curl -sf http://localhost:3000/api/health >/dev/null && break
  [ "$i" -eq 10 ] && { log "dashboard server failed, see logs/dashboard-server.log"; exit 1; }
  sleep 1
done

log "everything up:"
log "  dashboard:  http://localhost:3000"
log "  opencode:   http://localhost:4096"
log "  logs:       ./logs/"
