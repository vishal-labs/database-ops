import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import pg from "pg";

const PORT = 3000;
const OPENCODE = process.env.OPENCODE_URL || "http://localhost:4096";
const DB = process.env.DATABASE_URL || "postgresql://vishal:password@192.168.64.4:5432/vishal";

const AGENT_PROMPT = q => `You are a SQL analytics agent for a PostgreSQL database. Use your Postgres MCP tools (list_schemas, list_objects, get_object_details, execute_sql) to inspect the schema and answer the user's question.

Rules:
- Write ONE read-only SELECT query that answers the question.
- Validate it by executing it with the MCP execute_sql tool.
- Keep result sets small: aggregate or LIMIT to at most 30 rows.
- Then reply with ONLY a single fenced json block, nothing else, shaped exactly like:
\`\`\`json
{
  "summary": "one-sentence plain-English answer",
  "sql": "the validated SELECT query",
  "chart": { "type": "bar" | "pie" | "line", "title": "chart title", "x": "category column name", "y": "numeric column name" }
}
\`\`\`
- chart.y may be null if the result is not chartable; the table will still be shown.
- Do NOT include data rows in your reply. No extra text outside the json block.

Question: ${q}`;

let sessionId = null;

const log = (...a) => console.log(new Date().toISOString(), ...a);

async function opencode(method, url, body) {
  log(`opencode -> ${method} ${url}`);
  let res;
  try {
    res = await fetch(OPENCODE + url, {
      method,
      headers: { "content-type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error(`opencode unreachable at ${OPENCODE} (${e.cause?.code || e.message}) — is 'opencode serve --port 4096' running?`);
  }
  const text = await res.text();
  if (!res.ok) throw new Error(`opencode ${method} ${url} -> ${res.status}: ${text.slice(0, 300)}`);
  log(`opencode <- ${res.status} ${url}`);
  return JSON.parse(text);
}

async function createSession() {
  const s = await opencode("POST", "/session", { title: "dashboard-app" });
  sessionId = s.id;
  log("session created:", sessionId);
  return sessionId;
}

async function getSession() {
  return sessionId ?? createSession();
}

function extractJson(text) {
  const m = text.match(/```json\s*([\s\S]*?)```/);
  if (!m) throw new Error("agent did not return a json block. reply was:\n" + text.slice(0, 500));
  return JSON.parse(m[1]);
}

async function askAgent(question) {
  log("asking agent:", question);
  const send = async () => {
    const id = await getSession();
    return opencode("POST", `/session/${id}/message`, {
      parts: [{ type: "text", text: AGENT_PROMPT(question) }],
    });
  };
  let msg;
  try {
    msg = await send();
  } catch (e) {
    // ponytail: stale session after opencode restart — recreate once, then give up
    log("message failed, recreating session:", e.message);
    sessionId = null;
    await createSession();
    msg = await send();
  }
  const text = (Array.isArray(msg.parts) ? msg.parts : [])
    .filter(p => p.type === "text")
    .map(p => p.text)
    .join("\n");
  log("agent reply (first 300):", JSON.stringify(text.slice(0, 300)));
  return extractJson(text);
}

async function queryRaw(sql) {
  const client = new pg.Client({ connectionString: DB });
  await client.connect();
  try {
    await client.query("BEGIN READ ONLY");
    const r = await client.query(sql);
    await client.query("ROLLBACK");
    return r;
  } finally {
    await client.end();
  }
}

async function runSql(sql) {
  log("running sql:", sql);
  const r = await queryRaw(sql);
  log(`sql ok: ${r.rowCount} rows`);
  // normalize rows to arrays (pg returns objects) so every consumer renders the same way
  return { columns: r.fields.map(f => f.name), rows: r.rows.map(row => r.fields.map(f => row[f.name])) };
}

const REL = {
  "employees.dept_id": "departments.id",
  "employees.manager_id": "employees.id",
  "products.supplier_id": "suppliers.id",
  "orders.customer_id": "customers.id",
  "order_items.order_id": "orders.id",
  "order_items.product_id": "products.id",
  "payments.order_id": "orders.id",
  "inventory.product_id": "products.id",
  "inventory.warehouse_id": "warehouses.id",
  "shipments.order_id": "orders.id",
  "shipments.warehouse_id": "warehouses.id",
};

async function erDiagram() {
  const r = await queryRaw(`SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' ORDER BY table_name, ordinal_position`);
  const tables = {};
  for (const row of r.rows) (tables[row.table_name] ??= []).push(row);
  let out = "erDiagram\n";
  for (const [t, cols] of Object.entries(tables)) {
    out += `  ${t} {\n` + cols.map(c => `    ${c.data_type.replace(/\s+/g, "_")} ${c.column_name}`).join("\n") + "\n  }\n";
  }
  for (const [from, to] of Object.entries(REL)) {
    const [ft, fc] = from.split(".");
    const [tt, tc] = to.split(".");
    if (tables[ft] && tables[tt]) out += `  ${tt} ||--o{ ${ft} : "${fc} -> ${tc}"\n`;
  }
  return out;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const json = (code, body) => {
    res.writeHead(code, { "content-type": "application/json", "access-control-allow-origin": "*" });
    res.end(JSON.stringify(body));
  };
  try {
    if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html"))
      return res.end(fs.readFileSync(path.join(import.meta.dirname, "public/index.html")));
    if (req.method === "GET" && url.pathname === "/api/health") {
      const [oc, db] = await Promise.allSettled([
        opencode("GET", "/global/health"),
        queryRaw("SELECT 1"),
      ]);
      return json(200, {
        opencode: oc.status === "fulfilled",
        db: db.status === "fulfilled",
        detail: {
          opencode: oc.status === "fulfilled" ? oc.value : String(oc.reason),
          db: db.status === "fulfilled" ? "connected" : String(db.reason),
        },
      });
    }
    if (req.method === "GET" && url.pathname === "/api/er")
      return json(200, { mermaid: await erDiagram() });
    if (req.method === "POST" && url.pathname === "/api/ask") {
      const { question } = await new Promise((ok, err) => {
        let b = "";
        req.on("data", c => (b += c));
        req.on("end", () => { try { ok(JSON.parse(b)); } catch (e) { err(e); } });
      });
      const plan = await askAgent(question);
      const data = await runSql(plan.sql);
      return json(200, { ...plan, ...data });
    }
    json(404, { error: "not found" });
  } catch (e) {
    log("request failed:", e.message);
    json(500, { error: String(e.message || e) });
  }
});

server.listen(PORT, () => console.log(`dashboard on http://localhost:${PORT}`));
