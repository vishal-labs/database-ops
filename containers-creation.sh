container run -d --name postgres-mcp \
  -e DATABASE_URI="postgresql://vishal:password@192.168.64.4:5432/vishal" \
  -p 8000:8000 \
  crystaldba/postgres-mcp --access-mode=restricted --transport=sse


container run -d --name postgres-container \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_USER=vishal \
  -p 5432:5432 \
  postgres:17-alpine
