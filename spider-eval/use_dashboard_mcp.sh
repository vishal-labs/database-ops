#!/bin/bash
# Reverses use_spider_mcp.sh: points postgres-mcp back at 'vishal' for normal
# dashboard use. Run this when you're done with a benchmark session.
set -euo pipefail
MCP_NAME="${MCP_CONTAINER:-postgres-mcp}"

echo "recreating $MCP_NAME against the 'vishal' database"
docker rm -f "$MCP_NAME" >/dev/null 2>&1 || true
docker run -d --name "$MCP_NAME" \
  -e DATABASE_URI="postgresql://vishal:password@host.docker.internal:5432/vishal" \
  -p 8000:8000 \
  crystaldba/postgres-mcp --access-mode=restricted --transport=sse
echo "$MCP_NAME now points back at 'vishal'. Dashboard is back to normal."
