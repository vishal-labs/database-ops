#!/bin/bash
# Stops the full stack. Containers are stopped (not removed) so data survives; ./start.sh brings it all back.
set -uo pipefail
cd "$(dirname "$0")"
log() { echo "[$(date '+%H:%M:%S')] $*"; }

pkill -f "node server.js" && log "dashboard server stopped" || log "dashboard server not running"
pkill -f "opencode serve" && log "opencode serve stopped" || log "opencode serve not running"

for name in postgres-mcp postgres-container; do
  if container ls --format json | grep -q "\"$name\""; then
    container stop "$name" >/dev/null && log "$name stopped"
  else
    log "$name not running"
  fi
done
