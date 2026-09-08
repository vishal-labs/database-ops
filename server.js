import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import pg from "pg";

const runCmd = promisify(execFile);

const PORT = 3000;
const OPENCODE = process.env.OPENCODE_URL || "http://localhost:4096";
const DB = process.env.DATABASE_URL || "postgresql://vishal:password@192.168.64.7:5432/vishal";
const PG_NAME = process.env.PG_CONTAINER || "postgres-container";
const MCP_NAME = process.env.MCP_CONTAINER || "postgres-mcp";
const SNAPDIR = path.join(import.meta.dirname, "snapshots");
const MIGDIR = path.join(import.meta.dirname, "migrations");
const MAX_PROGRESS = 60; // cap progress events per question

const COMMON_RULES = `Rules:
- Produce ONE SQL statement.
- NEVER assume table or column names. First run list_objects + get_object_details via MCP to confirm the actual schema, then write SQL against it, then validate the final statement with execute_sql before replying.
- Keep result sets small: aggregate or LIMIT to at most 30 rows.
- Then reply with ONLY a single fenced json block, nothing else, shaped exactly like:
\`\`\`json
{
  "summary": "one-sentence plain-English answer",
  "sql": "the single SQL statement",
  "chart": { "type": "bar" | "pie" | "line", "title": "chart title", "x": "category column name", "y": "numeric column name" }
}
\`\`\`
- chart.y may be null if the result is not chartable; the table will still be shown.
- Do NOT include data rows in your reply. No extra text outside the json block.`;

const PROMPTS = {
  read: q => `You are a SQL analytics agent for a PostgreSQL database. Use your Postgres MCP tools (list_schemas, list_objects, get_object_details, execute_sql) to inspect the schema and answer the user's question.

Rules:
- Write ONE read-only SELECT query that answers the question.
- Validate it by executing it with the MCP execute_sql tool.
${COMMON_RULES}

Question: ${q}`,
  write: q => `You are a SQL agent for a PostgreSQL database. Use your Postgres MCP tools (list_schemas, list_objects, get_object_details, execute_sql) to inspect the schema and fulfil the user's request.

Rules:
- The statement may be a SELECT or a data/schema change (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, TRUNCATE) — whatever the request needs.
- The application executes your statement AFTER user confirmation. Do NOT execute INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/TRUNCATE with the MCP tools (they are blocked there); use MCP execute_sql only to validate SELECT queries.
${COMMON_RULES}

Request: ${q}`,
};

const MIGRATE_PROMPT = q => `You are a database migration agent for a PostgreSQL database. Use your Postgres MCP tools (list_schemas, list_objects, get_object_details) to inspect the current schema, then draft a SQL migration that fulfils the request.

Rules:
- The migration SQL may contain MULTIPLE statements separated by semicolons.
- Prefer IF EXISTS / IF NOT EXISTS guards so the migration is idempotent.
- Do NOT execute any write statement via MCP; use execute_sql only to inspect the schema or validate SELECT queries.
- The migration is saved to a file and applied by a separate tracked migrator, not by you.
- Reply with ONLY a single fenced json block, nothing else:
\`\`\`json
{ "summary": "one-sentence description of what this migration does", "sql": "the migration SQL (may be multi-statement)" }
\`\`\`

Request: ${q}`;

let sessionId = null;
let mode = "read"; // resets to read-only on restart by design
let busy = false;  // one agent round at a time — concurrent asks share one session history and corrupt each other

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

const getSession = () => sessionId ?? createSession();

function extractJson(text) {
  const m = text.match(/```json\s*([\s\S]*?)```/);
  if (!m) throw new Error("agent did not return a json block. reply was:\n" + text.slice(0, 500));
  return JSON.parse(m[1]);
}

async function askAgent(promptText) {
  log("asking agent:", JSON.stringify(promptText.slice(0, 120)));
  const send = async () => {
    const id = await getSession();
    return opencode("POST", `/session/${id}/message`, {
      parts: [{ type: "text", text: promptText }],
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

async function withAgentBusy(fn) {
  if (busy) throw Object.assign(new Error("agent is already working on a request — wait for it to finish"), { status: 409 });
  busy = true;
  try { return await fn(); } finally { busy = false; }
}

// Subscribe to opencode's SSE bus and surface this session's MCP tool activity.
function watchEvents(sessionId, onEvent, signal) {
  (async () => {
    try {
      const res = await fetch(OPENCODE + "/event", { signal });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "", last = "", count = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (!line.startsWith("data: ")) continue;
          try {
            const e = JSON.parse(line.slice(6));
            const p = e.properties || {};
            const part = p.part;
            if (p.sessionID !== sessionId || part?.type !== "tool") continue;
            const status = part.state?.status;
            if (!status || status === "pending" || count >= MAX_PROGRESS) continue;
            const key = part.tool + ":" + status;
            if (key === last) continue;
            last = key;
            count++;
            onEvent({ tool: part.tool, status });
          } catch {}
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") log("event stream ended:", e.message);
    }
  })();
}

// Classify agent SQL without a parser dependency: strip comments/strings, then
// check for multiple statements and leading keyword. ponytail: simple scanner,
// swap for pglast if adversarial SQL becomes a real threat model.
function classifySql(sql) {
  const s = String(sql || "")
    .replace(/--[^\n]*/g, " ")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/'(?:[^']|'')*'/g, "''")
    .trim();
  if (!s) return "unknown";
  if (/;/.test(s.replace(/;\s*$/, ""))) return "multi";
  const first = s.replace(/^\(+/, "").slice(0, 10).toLowerCase();
  if (/^(select|with|explain)\b/.test(first)) return "read";
  if (/^(insert|update|delete|truncate|create|alter|drop|grant|revoke|comment|vacuum|analyze|reindex)\b/.test(first)) return "write";
  if (/^(begin|commit|rollback|savepoint|release|prepare|execute|call|do|set|reset|copy|listen|notify|lock)\b/.test(first)) return "control";
  return "unknown";
}

async function execTx(sql, writable) {
  const client = new pg.Client({ connectionString: DB });
  await client.connect();
  const run = async inTx => {
    if (inTx) await client.query(writable ? "BEGIN" : "BEGIN READ ONLY");
    const r = await client.query(sql);
    if (inTx) await client.query(writable ? "COMMIT" : "ROLLBACK");
    return r;
  };
  try {
    try {
      return await run(true); // reads: BEGIN READ ONLY; writes: BEGIN/COMMIT
    } catch (e) {
      try { await client.query("ROLLBACK"); } catch {}
      // some write statements (CREATE DATABASE, VACUUM, ...) cannot run in a transaction block
      if (writable && /inside a transaction/i.test(e.message)) {
        log("statement cannot run in a transaction; retrying autocommit");
        return await run(false);
      }
      throw e;
    }
  } finally {
    await client.end();
  }
}

async function runSql(sql, writable = false) {
  log(writable ? "running WRITE:" : "running sql:", sql);
  const r = await execTx(sql, writable);
  log(`sql ok: ${r.rowCount} rows`);
  // normalize rows to arrays (pg returns objects) so every consumer renders the same way
  return {
    columns: r.fields.map(f => f.name),
    rows: r.rows.map(row => r.fields.map(f => row[f.name])),
    rowCount: r.rowCount,
  };
}

async function erDiagram() {
  const r = await execTx(`SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' ORDER BY table_name, ordinal_position`, false);
  const tables = {};
  for (const row of r.rows) (tables[row.table_name] ??= []).push(row);
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

function readBody(req) {
  return new Promise((ok, err) => {
    let b = "";
    req.on("data", c => (b += c));
    req.on("end", () => { try { ok(JSON.parse(b || "{}")); } catch (e) { err(e); } });
  });
}

async function planAndRun(question, currentMode, onProgress) {
  let extra = "";
  for (let attempt = 1; attempt <= 2; attempt++) {
    const plan = await askAgent(extra ? PROMPTS[currentMode](question + extra) : PROMPTS[currentMode](question));
    if (!plan.sql || typeof plan.sql !== "string")
      throw new Error(`agent could not produce SQL: ${plan.summary || "(no summary)"}`);
    const kind = classifySql(plan.sql);
    if (kind === "unknown") throw new Error(`agent returned unclassifiable SQL: ${String(plan.sql).slice(0, 120)}`);
    if (kind === "read") {
      try {
        return { ...plan, ...await runSql(plan.sql, false) };
      } catch (e) {
        log(`sql failed (attempt ${attempt}):`, e.message);
        onProgress?.({ stage: "retrying" });
        // ponytail: one retry with the DB error fed back; more retries = more latency, rarely helps
        extra = `\n\nIMPORTANT: your previous statement failed with this database error — fix the statement and revalidate it:\n${e.message}\nPrevious statement: ${plan.sql}`;
        continue;
      }
    }
    if (currentMode !== "write") throw new Error(`agent produced a ${kind} statement but write mode is off`);
    if (kind !== "write") throw new Error(`refusing to run ${kind} statement: ${String(plan.sql).slice(0, 120)}`);
    log("write plan needs confirmation:", plan.sql);
    return { ...plan, needsConfirmation: true, columns: [], rows: [], rowCount: 0 };
  }
  throw new Error("agent could not produce working SQL after one retry");
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
        execTx("SELECT 1", false),
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

    if (req.method === "GET" && url.pathname === "/api/mode")
      return json(200, { mode });

    if (req.method === "POST" && url.pathname === "/api/mode") {
      const { mode: m } = await readBody(req);
      if (m !== "read" && m !== "write") return json(400, { error: "mode must be 'read' or 'write'" });
      mode = m;
      log("mode set to:", mode);
      return json(200, { mode });
    }

    if (req.method === "POST" && url.pathname === "/api/ask") {
      if (busy) return json(409, { error: "agent is already working on a question — wait for it to finish" });
      const { question, stream } = await readBody(req);
      if (!question?.trim()) return json(400, { error: "question required" });
      busy = true;

      if (!stream) {
        try {
          return json(200, await planAndRun(question, mode));
        } finally { busy = false; }
      }

      // streamed variant: NDJSON lines of {progress:{tool,status}} then {result:{...}} or {error}
      res.writeHead(200, { "content-type": "application/x-ndjson", "cache-control": "no-cache" });
      const send = o => res.write(JSON.stringify(o) + "\n");
      const sid = await getSession();
      const ac = new AbortController();
      watchEvents(sid, p => send({ progress: p }), ac.signal);
      // client walked away — abort the orphaned agent round so it can't poison the session
      res.on("close", () => {
        if (!res.writableEnded) {
          log("client disconnected mid-ask; aborting agent round");
          ac.abort();
          opencode("POST", `/session/${sid}/abort`, {}).catch(() => {});
        }
      });
      try {
        send({ result: await planAndRun(question, mode, p => send({ progress: p })) });
      } catch (e) {
        send({ error: String(e.message || e) });
      } finally {
        busy = false;
      }
      ac.abort();
      return res.end();
    }

    if (req.method === "POST" && url.pathname === "/api/newsession") {
      sessionId = null;
      log("session reset requested — fresh session on next ask");
      return json(200, { ok: true });
    }

    if (req.method === "POST" && url.pathname === "/api/execute") {
      const { sql } = await readBody(req);
      if (mode !== "write") return json(403, { error: "write mode is off" });
      const kind = classifySql(sql);
      if (kind !== "write") return json(400, { error: `refusing to run ${kind} statement` });
      return json(200, await runSql(sql, true));
    }

    // ---------- manage: snapshots ----------
    if (req.method === "GET" && url.pathname === "/api/snapshots") {
      fs.mkdirSync(SNAPDIR, { recursive: true });
      const snapshots = fs.readdirSync(SNAPDIR)
        .filter(f => f.endsWith(".sql"))
        .map(f => {
          const st = fs.statSync(path.join(SNAPDIR, f));
          return { name: f, size: st.size, created: st.mtime };
        })
        .sort((a, b) => b.created - a.created);
      return json(200, { snapshots });
    }

    if (req.method === "POST" && url.pathname === "/api/snapshot") {
      fs.mkdirSync(SNAPDIR, { recursive: true });
      const name = `db-${new Date().toISOString().replace(/[:T]/g, "-").slice(0, 16)}.sql`;
      const { stdout } = await runCmd("container", ["exec", PG_NAME, "pg_dump", "-U", "vishal", "-d", "vishal"], { maxBuffer: 1e9 });
      if (!stdout.trim()) throw new Error("pg_dump produced no output");
      fs.writeFileSync(path.join(SNAPDIR, name), stdout);
      log("snapshot saved:", name, `${(stdout.length / 1024).toFixed(1)} KB`);
      return json(200, { name, size: stdout.length });
    }

    if (req.method === "POST" && url.pathname === "/api/restore") {
      if (mode !== "write") return json(403, { error: "restore requires write mode (it drops the current database)" });
      const { file } = await readBody(req);
      if (typeof file !== "string" || file.includes("/") || file.includes("..") || !file.endsWith(".sql"))
        return json(400, { error: "invalid snapshot name" });
      const dump = path.join(SNAPDIR, file);
      if (!fs.existsSync(dump)) return json(404, { error: "snapshot not found" });
      log("RESTORING from", file, "(drops current database)");
      const output = await new Promise((ok, err) => {
        const proc = spawn("container", ["exec", "-i", PG_NAME, "sh", "-c",
          'psql -U vishal -d postgres -q -c "DROP DATABASE IF EXISTS vishal WITH (FORCE)" && createdb -U vishal vishal && psql -U vishal -d vishal -q -v ON_ERROR_STOP=1']);
        fs.createReadStream(dump).pipe(proc.stdin);
        let out = "";
        proc.stdout.on("data", d => (out += d));
        proc.stderr.on("data", d => (out += d));
        proc.on("close", code => (code ? err(new Error(out.slice(-500))) : ok(out)));
      });
      // recreate the MCP container connection: restart it so its pool points at the fresh database
      await runCmd("container", ["restart", MCP_NAME]).catch(e => log("MCP restart failed:", e.message));
      log("restore complete");
      return json(200, { ok: true, output: output.slice(-500) });
    }

    // ---------- manage: migrations ----------
    if (req.method === "GET" && url.pathname === "/api/migrations") {
      fs.mkdirSync(MIGDIR, { recursive: true });
      let applied = [];
      try {
        applied = (await execTx("SELECT name FROM schema_migrations", false)).rows.map(r => r.name);
      } catch { applied = []; }
      const migrations = fs.readdirSync(MIGDIR).filter(f => f.endsWith(".sql")).sort()
        .map(f => ({ name: f, applied: applied.includes(f) }));
      return json(200, { migrations });
    }

    if (req.method === "POST" && url.pathname === "/api/migrate/draft") {
      const { request } = await readBody(req);
      if (!request?.trim()) return json(400, { error: "request required" });
      const plan = await withAgentBusy(() => askAgent(MIGRATE_PROMPT(request)));
      if (!plan.sql || typeof plan.sql !== "string")
        throw new Error(`agent could not draft a migration: ${plan.summary || "(no summary)"}`);
      return json(200, { summary: plan.summary || "", sql: plan.sql });
    }

    if (req.method === "POST" && url.pathname === "/api/migrate/save") {
      const { summary, sql } = await readBody(req);
      if (!sql?.trim()) return json(400, { error: "sql required" });
      fs.mkdirSync(MIGDIR, { recursive: true });
      const existing = fs.readdirSync(MIGDIR).filter(f => f.endsWith(".sql"));
      const num = String(existing.length + 1).padStart(3, "0");
      const slug = String(summary || "migration").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 40) || "migration";
      const name = `${num}_${slug}.sql`;
      fs.writeFileSync(path.join(MIGDIR, name), `-- drafted by agent: ${summary || "(unlabeled)"}\n${sql.trim()}\n`);
      log("migration saved:", name);
      return json(200, { name });
    }

    if (req.method === "POST" && url.pathname === "/api/migrate/apply") {
      const { stdout } = await runCmd("bash", [path.join(import.meta.dirname, "migrate.sh")], { cwd: import.meta.dirname, maxBuffer: 1e7 });
      log("migrations applied");
      return json(200, { output: stdout });
    }

    if (req.method === "GET" && url.pathname === "/api/er")
      return json(200, { mermaid: await erDiagram() });

    json(404, { error: "not found" });
  } catch (e) {
    log("request failed:", e.message);
    json(500, { error: String(e.message || e) });
  }
});

server.listen(PORT, () => console.log(`dashboard on http://localhost:${PORT}`));
