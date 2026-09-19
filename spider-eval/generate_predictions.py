#!/usr/bin/env python3
"""
Generates predicted SQL for Spider questions using the project's opencode agent
(one-shot, schema given in the prompt) and appends them to a resumable JSONL file.

Prereqs (same as run.md): `opencode serve --port 4096` running, GEMINI_API_KEY /
OPENCODE_MODEL exported in that shell.

Usage:
  python3 generate_predictions.py --dataset-dir dataset --split dev.json \
      --out results/predictions.jsonl [--limit 50] [--start 0] [--db concert_singer]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from schema_utils import load_tables, schema_text  # noqa: E402
from llm_client import new_session, ask, extract_sql  # noqa: E402

PROMPT_TMPL = """You are a text-to-SQL engine for SQLite databases.

{schema}

Question: {question}

Rules:
- Output ONE valid SQLite SQL query that answers the question, using only the tables/columns listed above.
- Do not invent columns or tables that are not listed.
- Reply with ONLY a single ```sql fenced code block containing the query -- no explanation, no extra text.
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
    ap.add_argument("--out", default="results/predictions.jsonl")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--db", default=None, help="only run questions for this db_id")
    ap.add_argument("--sleep", type=float, default=0.2, help="seconds between calls")
    args = ap.parse_args()

    dataset_dir = Path(args.dataset_dir)
    tables = load_tables(dataset_dir / "tables.json")
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
            if args.db and ex["db_id"] != args.db:
                continue

            db_id = ex["db_id"]
            if db_id not in tables:
                rec = {"idx": idx, "db_id": db_id, "question": ex["question"],
                       "predicted_sql": "", "error": f"db_id {db_id} not in tables.json"}
            else:
                schema = schema_text(tables[db_id])
                prompt = PROMPT_TMPL.format(schema=schema, question=ex["question"])
                try:
                    sid = new_session(f"spider-eval-{idx}")
                    reply = ask(sid, prompt)
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
