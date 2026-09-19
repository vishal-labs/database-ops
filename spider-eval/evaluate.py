#!/usr/bin/env python3
"""
Scores predictions.jsonl by EXECUTION ACCURACY: run the predicted SQL and the gold SQL
against the matching Spider SQLite database and compare result sets.

Usage:
  python3 evaluate.py --dataset-dir dataset --gold dev_gold.sql \
      --predictions results/predictions.jsonl --out results/report
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db_utils import execute, rows_match, is_order_sensitive  # noqa: E402


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
    ap.add_argument("--predictions", default="results/predictions.jsonl")
    ap.add_argument("--out", default="results/report")
    ap.add_argument("--timeout", type=float, default=15)
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
    per_db = defaultdict(lambda: [0, 0])  # [correct, total]

    for idx, (gold_sql, db_id) in enumerate(gold):
        db_path = dataset_dir / "database" / db_id / f"{db_id}.sqlite"
        gold_rows, gold_err = execute(db_path, gold_sql, args.timeout)
        pred = preds.get(idx)

        if pred is None:
            rows_out.append({"idx": idx, "db_id": db_id, "match": False,
                              "gold_sql": gold_sql, "predicted_sql": "",
                              "pred_error": "no prediction", "gold_error": gold_err})
            per_db[db_id][1] += 1
            continue

        pred_sql = pred.get("predicted_sql", "")
        pred_rows, pred_err = execute(db_path, pred_sql, args.timeout)
        order_sensitive = is_order_sensitive(gold_sql)
        match = (pred_err is None and gold_err is None
                 and rows_match(pred_rows, gold_rows, order_sensitive))

        rows_out.append({"idx": idx, "db_id": db_id, "match": match,
                          "gold_sql": gold_sql, "predicted_sql": pred_sql,
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
        "per_db": {k: {"correct": v[0], "total": v[1],
                        "accuracy": (v[0] / v[1]) if v[1] else 0}
                   for k, v in sorted(per_db.items())},
    }
    with open(f"{out_base}.json", "w") as f:
        json.dump(report, f, indent=2)

    with open(f"{out_base}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["idx", "db_id", "match", "gold_sql",
                                           "predicted_sql", "pred_error", "gold_error"])
        w.writeheader()
        w.writerows(rows_out)

    print(f"Execution accuracy: {correct}/{total} = {(correct/total*100 if total else 0):.2f}%")
    print(f"Report written to {out_base}.json and {out_base}.csv")

    if per_db:
        worst = sorted(per_db.items(), key=lambda kv: kv[1][0] / kv[1][1] if kv[1][1] else 1)[:10]
        print("\nLowest-accuracy databases:")
        for db_id, (c, t) in worst:
            print(f"  {db_id}: {c}/{t} ({(c/t*100 if t else 0):.1f}%)")


if __name__ == "__main__":
    main()
