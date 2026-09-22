#!/usr/bin/env python3
"""
Scores predictions.jsonl (from generate_predictions_mcp.py) by EXECUTION ACCURACY,
against the Postgres 'spider' database (each db_id = one schema). This is the scoring
counterpart to generate_predictions_mcp.py / load_spider_to_postgres.py.

Usage:
  python3 evaluate_pg.py --dataset-dir dataset --gold dev_gold.sql \
      --database-url postgresql://vishal:password@127.0.0.1:5432/spider \
      --predictions results/predictions_mcp.jsonl --out results/report_mcp
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_compare import (execute, rows_match, is_order_sensitive,  # noqa: E402
                         get_case_map, get_column_types, get_column_types_by_table,
                         rewrite_gold_sql)


def load_gold(dataset_dir, gold_file):
    gold = []
    with open(Path(dataset_dir) / gold_file) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            sql, db_id = line.rsplit("\t", 1)
            gold.append((sql.strip(), db_id.strip()))
    return gold


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dir", default="dataset")
    ap.add_argument("--gold", default="dev_gold.sql")
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--predictions", default="results/predictions_mcp.jsonl")
    ap.add_argument("--out", default="results/report_mcp")
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--no-case-fix", action="store_true",
                     help="disable gold-SQL case/type rewriting (use if your schema was "
                          "loaded/reloaded with the lowercasing fix and gold SQL "
                          "already matches as-is)")
    args = ap.parse_args()

    dataset_dir = Path(args.dataset_dir)
    gold = load_gold(dataset_dir, args.gold)

    preds = {}
    with open(args.predictions) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            preds[r["idx"]] = r

    rows_out = []
    per_db = defaultdict(lambda: [0, 0])
    skipped = 0

    for idx, (gold_sql, db_id) in enumerate(gold):
        pred = preds.get(idx)
        if pred is None:
            # No prediction was generated for this question (e.g. only a subset
            # of the dataset has been run so far) -- excluded entirely from
            # accuracy, not counted as wrong, not counted in the denominator,
            # and not scored against the database at all.
            skipped += 1
            continue

        gold_sql_exec = gold_sql
        if not args.no_case_fix:
            try:
                case_map = get_case_map(args.database_url, db_id)
                type_map = get_column_types(args.database_url, db_id)
                table_types = get_column_types_by_table(args.database_url, db_id)
                gold_sql_exec = rewrite_gold_sql(gold_sql, case_map, type_map, table_types)
            except Exception as e:
                # schema lookup failed (e.g. db_id doesn't exist) -- fall back to
                # running gold_sql as-is; execute() below will report the real error
                gold_sql_exec = gold_sql

        gold_rows, gold_err = execute(args.database_url, db_id, gold_sql_exec, args.timeout)

        pred_sql = pred.get("predicted_sql", "")
        pred_rows, pred_err = execute(args.database_url, db_id, pred_sql, args.timeout)
        order_sensitive = is_order_sensitive(gold_sql)
        match = (pred_err is None and gold_err is None
                 and rows_match(pred_rows, gold_rows, order_sensitive))

        rows_out.append({"idx": idx, "db_id": db_id, "match": match,
                          "gold_sql": gold_sql, "gold_sql_executed": gold_sql_exec,
                          "predicted_sql": pred_sql,
                          "pred_error": pred_err, "gold_error": gold_err})
        per_db[db_id][1] += 1
        if match:
            per_db[db_id][0] += 1

    total = len(rows_out)
    correct = sum(1 for r in rows_out if r["match"])

    out_base = Path(args.out)
    out_base.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "overall_accuracy": (correct / total) if total else 0,
        "correct": correct,
        "total": total,
        "skipped_no_prediction": skipped,
        "gold_dataset_size": len(gold),
        "per_db": {k: {"correct": v[0], "total": v[1],
                        "accuracy": (v[0] / v[1]) if v[1] else 0}
                   for k, v in sorted(per_db.items())},
    }
    with open(f"{out_base}.json", "w") as f:
        json.dump(report, f, indent=2)

    with open(f"{out_base}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["idx", "db_id", "match", "gold_sql", "gold_sql_executed",
                                           "predicted_sql", "pred_error", "gold_error"])
        w.writeheader()
        w.writerows(rows_out)

    print(f"Execution accuracy: {correct}/{total} = {(correct/total*100 if total else 0):.2f}%  "
          f"(scored {total} of {len(gold)} gold questions; {skipped} skipped, no prediction)")
    print(f"Report written to {out_base}.json and {out_base}.csv")

    if per_db:
        worst = sorted(per_db.items(), key=lambda kv: kv[1][0] / kv[1][1] if kv[1][1] else 1)[:10]
        print("\nLowest-accuracy databases:")
        for db_id, (c, t) in worst:
            print(f"  {db_id}: {c}/{t} ({(c/t*100 if t else 0):.1f}%)")


if __name__ == "__main__":
    main()