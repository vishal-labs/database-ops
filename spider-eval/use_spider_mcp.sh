#!/bin/bash
# Retargets the dashboard's postgres-mcp container at the 'spider' database instead
# of 'vishal', for the duration of a benchmark run. Same container name/port as
# containers-creation.sh / start.sh -- this is the exact same MCP server slot
# opencode already has registered, just pointed somewhere else. Run
# use_dashboard_mcp.sh afterwards to point it back at 'vishal' for normal dashboard use.
set -euo pipefail
MCP_NAME="${MCP_CONTAINER:-postgres-mcp}"

echo "recreating $MCP_NAME against the 'spider' database"
docker rm -f "$MCP_NAME" >/dev/null 2>&1 || true
docker run -d --name "$MCP_NAME" \
  -e DATABASE_URI="postgresql://vishal:password@host.docker.internal:5432/spider" \
  -p 8000:8000 \
  crystaldba/postgres-mcp --access-mode=restricted --transport=sse
echo "$MCP_NAME now points at 'spider'. Run generate_predictions_mcp.py now."
echo "When done, run ./use_dashboard_mcp.sh to point it back at 'vishal'."
