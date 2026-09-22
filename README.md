# Agentic Postgres Analytics Dashboard — Technical Report

**Project:** natural-language analytics over a containerized PostgreSQL database, driven by an AI agent through the Model Context Protocol (MCP)
**Stack:** Apple Containers · PostgreSQL 17 · Postgres MCP Pro · opencode (agent runtime) · Node.js · vanilla HTML/JS + Mermaid

---

## Abstract

This project reproduces — with entirely free and open-source components — the "agentic database" experience demonstrated by commercial data-warehouse vendors: a user types a question in plain English, an AI agent inspects the database metadata, writes and validates SQL, executes it, and the results are rendered as an answer, a chart, and a shareable dashboard view — plus a standalone ER diagram generated from live schema metadata. **Privacy** was the main focus and the entire DB engine is built around it, none of the Query Responses, parse through the Agent. 

The system runs entirely on a local Mac. A PostgreSQL database and an MCP server each run as containers on Apple's native container runtime; the agent reasoning is delegated to an opencode session; a thin Node.js server orchestrates the flow and enforces a strict safety model; the frontend is a single static HTML page. Three properties were treated as non-negotiable design constraints: **(1)** the agent can *plan* SQL but never *executes* writes — every write passes through an explicit user-confirmation gate backed by a SQL classifier and read-only transactions; **(2)** result data flows to the UI deterministically (browser → server → database) without being relayed through the LLM; **(3)** the whole stack is reproducible with two scripts (`start.sh` / `stop.sh`) that are resilient to reboots, container IP changes, and stale connections.

---
<img width="415" height="676" alt="image" src="https://github.com/user-attachments/assets/40edace1-0915-42b7-b976-9a7e792f2da9" />

---
## 1. Motivation

The reference demo (Exasol + Claude Code) showed an agent that: (a) had access to table metadata across many tables, (b) prepared and executed SQL from natural language, and (c) built dashboards on top of the results. Exasol itself is a paid, proprietary analytics database — the *demo pattern*, however, is not Exasol-specific. This project implements the same pattern on commodity PostgreSQL at zero license cost.

## 2. Architecture

```
┌────────────┐  question   ┌──────────────┐  prompt    ┌─────────────────┐
│  Browser   │ ──────────► │  Node server │ ─────────► │ opencode server │
│ index.html │  NDJSON ◄── │   :3000      │  HTTP API  │  :4096 (agent)  │
└────────────┘  (stream)   └──────┬───────┘            └───────┬─────────┘
      │                           │                            │ MCP (SSE)
      │ rows/tables/charts        │ BEGIN READ ONLY            ▼
      │                           ▼                    ┌──────────────────┐
      │                    ┌──────────────┐             │ postgres-mcp     │
      └───────────────────►│  PostgreSQL  │◄────────────│ container :8000  │
                           │  container   │  psycopg3   └──────────────────┘
                           │  :5432       │
                           └──────────────┘
```

Two deliberately separated data paths:

- **Planning path (agent):** browser → node server → opencode server → LLM → MCP server container → PostgreSQL. The agent touches metadata and *validates* queries here, but result rows never travel back up this path (they would waste tokens and risk lossy relay).
- **Execution path (deterministic):** browser → node server → PostgreSQL directly via `pg`. Once the agent has produced and validated a statement, the server executes it itself and hands raw rows/columns to the frontend. LLM output is limited to a small JSON plan (`summary`, `sql`, `chart`).

In total, agent-side data traverses **six layers** (browser → app server → opencode → LLM → MCP server → database); the result data takes the short three-hop path.

## 3. Components

| Layer | Choice | Why |
|---|---|---|
| Container runtime | **Apple Containers** (`container` CLI) | macOS-native, VM-per-container via Virtualization.framework, no Docker Desktop daemon or license; images from Docker Hub (`postgres:17-alpine`, `crystaldba/postgres-mcp`) |
| Database | PostgreSQL 17 (container) | target of the demo; any Postgres works |
| MCP server | **Postgres MCP Pro** (`crystaldba/postgres-mcp`, MIT) | actively maintained, tool-based API, read-only *restricted* mode, schema-intelligence + health/tuning tools |
| Agent runtime | **opencode** (`opencode serve --port 4096`) | exposes the session + MCP client over a local HTTP API; the same agent/tools already used interactively in the CLI power the app |
| App server | Node.js, single file, only dep `pg` | proxies questions to the agent session, executes SQL, serves the UI |
| Frontend | Single `public/index.html`, Mermaid 11 CDN | zero build step, dark minimal UI |
| Orchestration | `start.sh` / `stop.sh` | idempotent full-stack lifecycle |

### Why Postgres MCP Pro (comparative)

| Server | Status | Model | Notes |
|---|---|---|---|
| Anthropic reference `server-postgres` | **archived** | read-only, schema as MCP resources | the npm page users find first is deprecated |
| DBHub (Bytebase) | active | multi-DB, minimal tools | good, but no Postgres-specific intelligence |
| **Postgres MCP Pro (Crystal DBA)** | active, MIT | read/write modes, tools-only | chosen: `restricted` mode (read-only transactions + SQL parsing that rejects transaction control), schema tools, plus future index-tuning/health features |

### SSE vs stdio transport

stdio (the common MCP setup) spawns one MCP process *per client* on the host. This project runs the MCP server as a **standalone container with SSE transport** (`--transport=sse`, port 8000): one long-lived server, shareable by multiple MCP clients (opencode CLI, the app, Claude Desktop), and it matches the "database lives in a container" model. The trade-off — connection staleness when the database container's IP changes — is handled in ops (§7).

## 4. End-to-end flow (read query)

1. User submits a question in the Dashboard tab; the frontend opens a streaming `POST /api/ask` and renders progress lines live.
2. The node server forwards the question to a persistent opencode session with a strict prompt ("inspect schema first, never assume tables, validate before replying, reply with only a JSON block").
3. The agent, inside opencode, calls MCP tools on the Postgres MCP container: `list_schemas` → `list_objects` → `get_object_details` → `execute_sql` (read-only validation). Each call streams back as a progress event (`→ get_object_details…`, `✓ execute_sql`).
4. The agent replies with `{summary, sql, chart}` — no data rows.
5. The server **classifies** the SQL (§6), executes it in a `BEGIN READ ONLY` transaction, and appends normalized `columns`/`rows`.
6. The frontend renders: plain-English answer, a Mermaid chart (bar/line/pie — `xychart-beta`), the SQL, and the result table.
7. If execution fails (schema drift, typos), the DB error is fed back to the agent for **one automatic retry**.

## 5. Features

- **Natural-language queries** with live streaming feedback (per-tool progress, retry notices) over NDJSON.
- **Auto-charts** — the agent proposes chart type/axes; the frontend rebuilds the Mermaid spec defensively (sanitized labels, capped points, positive-only pie values, guaranteed-valid `xychart-beta` syntax) and verifies rendering; on failure the error is shown inline, never a blank pane.
- **ER tab** — an `erDiagram` built *deterministically* from `information_schema` (tables, columns, types + FK relations), no LLM involved; it can never drift from the real schema.
- **Health checks** — the header dot polls `/api/health` (pings both the opencode server and the database) every 15 s: green = both reachable, red with a tooltip naming exactly which side is down and how to fix it.
- **Write mode** (§6) — opt-in, per-session, with a confirmation gate.
- **Ops scripts** — `start.sh` (starts/creates containers, waits for Postgres readiness, reseeds only if empty, recreates the MCP container with the *current* DB IP, launches opencode + app server, logs to `./logs/`) and `stop.sh` (symmetric teardown; containers are stopped, not removed, so data survives).

## 6. Safety model (read vs write mode)

**Read mode (default, survives restarts):**
- Agent prompt allows only SELECTs; MCP container itself runs `--access-mode=restricted`.
- Server executes everything inside `BEGIN READ ONLY … ROLLBACK` — the *database* enforces read-only even if the agent's SQL tries otherwise.

**Write mode (explicit toggle):**
- The critical design decision: **the agent never gains write capability.** The MCP container stays restricted; the agent can only *prepare* a write statement. The sole write path is: agent produces statement → server classifier validates it → **user clicks "Run statement"** in the browser → server executes it in an explicit `BEGIN … COMMIT` transaction.
- Guard rails (all verified by test):
  - single-statement scanner — rejects multi-statement payloads (`UPDATE …; DROP TABLE …`), strips comments and string literals first (a `;` inside a literal is not a statement separator);
  - transaction-control keywords (`COMMIT`, `ROLLBACK`, `SET`, `LOCK`, `COPY`, …) are refused outright;
  - `/api/execute` refuses read statements and returns 403 when write mode is off;
  - write mode resets to read on any app-server restart.

## 7. Operational lessons (failure → fix, all reproduced in testing)

| Failure observed | Root cause | Fix |
|---|---|---|
| UI rendered `r.map is not a function` | `pg` returns row *objects*; renderer expected arrays | rows normalized to arrays at the source |
| Mermaid "no diagram type detected" | wrapper `<div>` passed to `mermaid.run()` instead of the `<pre>` | pass the diagram element itself |
| xychart parse error | invalid bare trailing `-->` on `y-axis` | emit doc-canonical `y-axis "label" 0 --> max` |
| MCP tools suddenly "connection down" | database container re-IP'd after restart; MCP container was *started*, not recreated, keeping a stale `DATABASE_URI` | `start.sh` always recreates the stateless MCP container with the live IP |
| Agent hallucinated schema (`order_details`, `o.order_id`) | prompt allowed assuming schema | hardened prompt: must call `list_objects`/`get_object_details` first; failed SQL retried once with the DB error fed back |
| Two concurrent questions corrupted one session | shared session history interleaved | busy flag serializes asks; client disconnect aborts the orphaned agent round (`POST /session/:id/abort`) |
| Health red after Mac reboot | Apple Containers don't auto-start; `/tmp` logs vanish; container IPs change | `start.sh`/`stop.sh`, logs in `./logs/`, IP resolved dynamically at startup |

## 8. Limitations & future work

- Single-user, localhost, no auth — fine for a local tool, not for exposure.
- Charts are Mermaid `xychart-beta` (bar/line/pie); richer visuals would warrant a chart library.
- Write mode is all-or-nothing; per-statement role whitelisting (e.g., allow INSERT, forbid DROP) is a natural next step.
- opencode sessions accumulate context; a session-rotations endpoint exists (`POST /api/newsession`) but has no UI yet.
- Index-tuning/health tools of Postgres MCP Pro require `pg_stat_statements` + `hypopg` extensions — not yet installed on the container.

## Appendix — artifacts

| File | Purpose |
|---|---|
| `server.js` | app server: agent proxy, streaming, classifier, executor, ER builder, health |
| `public/index.html` | dashboard UI (Dashboard + ER tabs, write toggle, progress, confirm gate) |
| `seed.sql` | 11-table fake enterprise dataset (FK web for multi-table joins) |
| `start.sh` / `stop.sh` | full-stack lifecycle |
| `verify-charts.mjs` | browser-level chart regression check (real Chrome + real Mermaid) |
| `containers-creation.sh` | original manual container setup commands |
