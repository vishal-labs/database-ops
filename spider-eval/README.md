# Spider benchmark harness — MCP-preserving version

This keeps your production architecture intact: the agent still discovers schema and
validates SQL through `postgres-mcp` (`list_schemas`, `list_objects`,
`get_object_details`, `execute_sql`), exactly like `server.js`'s `PROMPTS.read`. The
only change is *what database that MCP server points at* while a benchmark runs.

## How Spider's 200 databases fit your one-Postgres-database design

Spider ships ~200 independent SQLite files, one schema each. Postgres has a native
concept for exactly this: **schemas within one database**. So:

- A new Postgres database, `spider`, is created on your *existing* `postgres-container`
  (it does not touch `vishal` or your seed data).
- Every Spider `db_id` becomes its own schema inside `spider`, e.g. schema
  `"concert_singer"`, `"academic"`, etc. — loaded from its SQLite file.
- Your existing `postgres-mcp` container is **retargeted** at `spider` instead of
  `vishal` for the duration of a benchmark run (same container name, same port, same
  MCP server opencode already has registered — just a different `DATABASE_URI`), then
  pointed back at `vishal` when you're done. `postgres-mcp`'s own `list_schemas` /
  `list_objects(schema_name=...)` tools already support scoping to one schema, so the
  agent can inspect exactly one Spider database per question without seeing the other
  199, and without any container churn per question.

This means **zero MCP container recreation per question** — only twice total, per
benchmark session (once to point at `spider`, once to point back at `vishal`).

## Files

| File | Purpose |
|---|---|
| `load_spider_to_postgres.py` | Loads every Spider SQLite db into its own Postgres schema in `spider` |
| `setup_spider_db.sh` | One-time: creates the `spider` database on your existing Postgres container |
| `use_spider_mcp.sh` | Retargets `postgres-mcp` at `spider` (run before a benchmark session) |
| `use_dashboard_mcp.sh` | Points `postgres-mcp` back at `vishal` (run after) |
| `generate_predictions_mcp.py` | Same agent path as the dashboard: asks opencode, which uses MCP tools to discover schema + validate SQL |
| `evaluate_pg.py` | Scores predictions by execution accuracy against `spider` (schema-scoped, read-only, timeout-bounded) |
| `pg_compare.py` | Shared Postgres execute/compare logic used by `evaluate_pg.py` |
| `llm_client.py` | Thin opencode HTTP client (session create + message), shared with the simpler path below |
| `generate_predictions.py`, `evaluate.py`, `db_utils.py`, `schema_utils.py` | A simpler, schema-in-prompt / SQLite-direct path — see "Why there are two paths" below |

## 1. Place the Spider dataset

Unzip Spider so you get:

```
spider-eval/dataset/
  database/<db_id>/<db_id>.sqlite   (one folder per db_id)
  dev.json
  dev_gold.sql
  tables.json          (not needed by the MCP path, only by the simpler path)
```

## 2. One-time setup

```bash
cd spider-eval
pip install -r requirements.txt      # psycopg2-binary

./setup_spider_db.sh                 # creates the 'spider' database, once

python3 load_spider_to_postgres.py --dataset-dir dataset \
    --database-url postgresql://vishal:password@127.0.0.1:5432/spider
```

`load_spider_to_postgres.py` reads each SQLite file directly (table list, columns,
types, primary keys via `PRAGMA`) — it does **not** depend on `tables.json`'s format,
so it's robust to whatever exact Spider release you downloaded. It's idempotent:
rerun anytime, or `--only <db_id> --recreate` to reload one database. Foreign keys
are skipped by default (`--with-fk` to attempt them; best-effort, failures are
non-fatal and just print a warning — you don't need them for scoring, only real
column/table names matter for the agent).

Sanity-check a couple of schemas landed correctly:

```bash
docker exec -it postgres-container psql -U vishal -d spider -c '\dn'
docker exec -it postgres-container psql -U vishal -d spider -c 'SELECT * FROM "concert_singer"."singer" LIMIT 3;'
```

## 3. Register `spider` with opencode (check this once)

Your dashboard's `opencode` already has `postgres-mcp` (port 8000) registered as an
MCP server somewhere in its config (commonly `opencode.json` in the project root, or
`~/.config/opencode/opencode.json`) — that's how `server.js`'s agent calls
`list_schemas` etc. today. Nothing needs to change there: `use_spider_mcp.sh` reuses
that exact same container name and port, just with a different `DATABASE_URI`, so
opencode keeps talking to the same MCP server entry throughout.

If you don't already have that entry (e.g. you're setting this up fresh), it should
look like:

```jsonc
// opencode.json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "postgres-mcp": {
      "type": "remote",
      "url": "http://localhost:8000/sse",
      "enabled": true
    }
  }
}
```

Run `opencode mcp list` to confirm the server name/status if you're not sure it's
already there.

## 4. Run a benchmark session

```bash
./use_spider_mcp.sh          # postgres-mcp now points at 'spider'

# smoke test: 5 questions from one database first
python3 generate_predictions_mcp.py --dataset-dir dataset --split dev.json \
    --out results/predictions_mcp.jsonl --db concert_singer --limit 5

# then the full dev set (resumable -- Ctrl-C and rerun picks up where it left off)
python3 generate_predictions_mcp.py --dataset-dir dataset --split dev.json \
    --out results/predictions_mcp.jsonl

./use_dashboard_mcp.sh       # postgres-mcp back to 'vishal' -- dashboard is normal again
```

Score it (this step only needs Postgres, not opencode/MCP, so you can run it any time
after — including with `postgres-mcp` already pointed back at `vishal`):

```bash
python3 evaluate_pg.py --dataset-dir dataset --gold dev_gold.sql \
    --database-url postgresql://vishal:password@127.0.0.1:5432/spider \
    --predictions results/predictions_mcp.jsonl --out results/report_mcp
```

Prints overall accuracy + worst-performing databases, and writes:
- `results/report_mcp.json` — overall + per-db accuracy
- `results/report_mcp.csv` — every question: gold SQL, predicted SQL, match/mismatch, error

## How correctness is judged

For each question: gold SQL and predicted SQL are both executed by `evaluate_pg.py`
directly (not through MCP) on a fresh, **read-only** Postgres connection (mirrors
`server.js`'s own `execTx(sql, false)` → `BEGIN READ ONLY`), with `search_path`
pinned to that question's schema and a `statement_timeout` so a runaway query can't
hang scoring. Predicted SQL from the agent is expected to be schema-qualified
(`"db_id"."table"`, per the prompt) so it resolves correctly regardless of
`search_path` — the pinned `search_path` is a belt-and-suspenders fallback for gold
SQL (which Spider writes with bare table names) and for predictions that forgot to
qualify.

Result rows are compared as sets (column order doesn't matter — `SELECT name, salary`
vs `SELECT salary, name` both count), with row order also compared when the gold
query has `ORDER BY`. This is a practical execution-accuracy scorer, not a
byte-for-byte port of `taoyds/spider`'s official `evaluation.py`.

I tested `load_spider_to_postgres.py` and `pg_compare.py` against a real local
Postgres instance with synthetic data (NULLs, commas and quotes in text fields,
column-order permutation, cross-schema isolation, read-only-write rejection, and
statement-timeout cutoff all verified) before handing this over — the real Spider
dataset will exercise far more SQL shapes than that, so treat the first run as a
shakedown and skim `report_mcp.csv` for surprising error strings before trusting the
headline number.

## Cost/time expectations

Spider's dev split is ~1034 questions. Unlike the schema-in-prompt path, each
question here is a full multi-turn agent round (list_schemas → list_objects →
get_object_details → execute_sql → reply), so expect several Gemini calls per
question and noticeably more wall-clock time than the simple path. Start with
`--db <one db_id> --limit 5` to confirm everything's wired correctly before running
the full set. `--sleep`/`--timeout` on `generate_predictions_mcp.py` are tuned wider
than the simple path's defaults for this reason.

## Why there are two paths

`generate_predictions.py` / `evaluate.py` (schema-in-prompt, direct SQLite execution)
from the first pass at this are still here as a **fast sanity-check path**: no
Postgres, no MCP, no container retargeting, useful for quickly gauging "can this LLM
write correct SQL at all" or A/B-ing prompt wording before spending the extra time/
MCP-round-trips on the full-fidelity run. `generate_predictions_mcp.py` /
`evaluate_pg.py` are what you asked for — the actual dashboard architecture,
unmodified, pointed at Spider.
