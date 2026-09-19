#!/usr/bin/env python3
"""
Generates predicted SQL for Spider questions the SAME WAY the live dashboard does:
the agent (via opencode) uses its Postgres MCP tools (list_schemas, list_objects,
get_object_details, execute_sql) to discover the schema itself and validate its SQL
-- nothing is spoon-fed. The only addition vs. server.js's own PROMPTS.read is
telling the agent which Postgres SCHEMA this question's tables live in, since Spider
puts every db_id in its own schema (see load_spider_to_postgres.py) rather than one
single "public" schema.

Prereqs:
  1. Spider loaded into Postgres schemas: python3 load_spider_to_postgres.py ...
  2. The dashboard's postgres-mcp container retargeted at the spider database:
     bash use_spider_mcp.sh   (see that script; flips it back with use_dashboard_mcp.sh)
  3. opencode serve --port 4096 running (same as run.md), with that postgres-mcp
     already registered as an MCP server in opencode's config -- same one the
     dashboard already uses, just pointed at a different Postgres database now.

Usage:
  python3 generate_predictions_mcp.py --dataset-dir dataset --split dev.json \
      --out results/predictions_mcp.jsonl [--limit 20] [--db concert_singer]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm_client import new_session, ask, extract_sql  # noqa: E402

PROMPT_TMPL = """You are a SQL analytics agent for a PostgreSQL database. Use your Postgres MCP tools (list_schemas, list_objects, get_object_details, execute_sql) to inspect the schema and answer the user's question.

Rules:
- All the tables needed for this question live in the Postgres schema "{db_id}" (not "public"). Scope every list_objects / get_object_details call to schema_name="{db_id}".
- Always qualify table references in SQL as "{db_id}"."table_name", so the query is correct regardless of search_path.
- NEVER assume table or column names -- call list_objects and get_object_details first to confirm what actually exists in schema "{db_id}".
- Write ONE read-only SELECT query that answers the question.
- Validate it by executing it with the MCP execute_sql tool before replying.
- Reply with ONLY a single fenced ```sql code block containing the query -- no explanation, no extra text.

Question: {question}
"""


def already_done(out_path):
    done = set()
    if out_path.exists():
        with open(out_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["idx"])
                except Exception:
                    pass
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dir", default="dataset")
    ap.add_argument("--split", default="dev.json")
    ap.add_argument("--out", default="results/predictions_mcp.jsonl")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--db", default=None, help="only run questions for this db_id")
    ap.add_argument("--sleep", type=float, default=0.5,
                     help="seconds between calls (MCP round trips are slower than "
                          "schema-in-prompt calls -- several tool calls per question)")
    ap.add_argument("--timeout", type=float, default=180,
                     help="seconds to wait for one agent turn (tool-calling takes longer)")
    args = ap.parse_args()

    dataset_dir = Path(args.dataset_dir)
    with open(dataset_dir / args.split) as f:
        examples = json.load(f)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = already_done(out_path)

    n_run = 0
    with open(out_path, "a") as out:
        for idx, ex in enumerate(examples):
            if idx < args.start:
                continue
            if args.limit is not None and n_run >= args.limit:
                break
            if idx in done:
                continue
            db_id = ex["db_id"]
            if args.db and db_id != args.db:
                continue

            prompt = PROMPT_TMPL.format(db_id=db_id, question=ex["question"])
            try:
                sid = new_session(f"spider-mcp-eval-{idx}")
                reply = ask(sid, prompt, timeout=args.timeout)
                sql = extract_sql(reply)
                rec = {"idx": idx, "db_id": db_id, "question": ex["question"],
                       "predicted_sql": sql, "error": None}
            except Exception as e:
                rec = {"idx": idx, "db_id": db_id, "question": ex["question"],
                       "predicted_sql": "", "error": str(e)}

            out.write(json.dumps(rec) + "\n")
            out.flush()
            n_run += 1
            print(f"[{idx}] {db_id}: {rec['predicted_sql'][:100] or rec['error']}")
            time.sleep(args.sleep)

    print(f"\nDone. {n_run} new predictions written to {out_path}")


if __name__ == "__main__":
    main()
