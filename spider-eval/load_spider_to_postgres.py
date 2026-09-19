#!/usr/bin/env python3
"""
Loads Spider SQLite databases into a single Postgres database, one Postgres SCHEMA
per db_id (so the existing postgres-mcp container's list_schemas/list_objects tools
"just work" against every Spider database without recreating any container).

Usage:
  python3 load_spider_to_postgres.py --dataset-dir dataset \
      --database-url postgresql://vishal:password@127.0.0.1:5432/spider

  # reload a single db_id (e.g. after fixing something)
  python3 load_spider_to_postgres.py --dataset-dir dataset \
      --database-url ... --only concert_singer --recreate
"""
import argparse
import csv
import io
import sqlite3
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# SQLite storage classes -> a reasonable Postgres type. SQLite is dynamically typed
# (type affinity, not enforcement) so this is deliberately permissive.
def pg_type(sqlite_type):
    t = (sqlite_type or "").upper()
    if "INT" in t:
        return "BIGINT"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if any(k in t for k in ("NUMERIC", "DECIMAL")):
        return "NUMERIC"
    if any(k in t for k in ("BOOL",)):
        return "BOOLEAN"
    if any(k in t for k in ("BLOB",)):
        return "BYTEA"
    return "TEXT"  # TEXT, VARCHAR, CHAR, DATE, DATETIME, or unknown affinity


def q(name):
    """Quote a Postgres identifier."""
    return '"' + name.replace('"', '""') + '"'


def sqlite_tables(sconn):
    cur = sconn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [r[0] for r in cur.fetchall()]


def load_one_db(pconn, db_id, sqlite_path, recreate, with_fk):
    sconn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    tables = sqlite_tables(sconn)
    if not tables:
        print(f"  [skip] {db_id}: no tables found in {sqlite_path}")
        return

    pcur = pconn.cursor()
    schema = db_id
    if recreate:
        pcur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    pcur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    fk_statements = []
    for table in tables:
        scur = sconn.cursor()
        scur.execute(f'PRAGMA table_info("{table}")')
        cols = scur.fetchall()  # cid, name, type, notnull, dflt_value, pk
        col_defs = []
        pk_cols = []
        for cid, name, ctype, notnull, dflt, pk in cols:
            col_defs.append(f'{q(name)} {pg_type(ctype)}')
            if pk:
                pk_cols.append(name)
        pk_clause = f", PRIMARY KEY ({', '.join(q(c) for c in pk_cols)})" if pk_cols else ""

        pcur.execute(f'DROP TABLE IF EXISTS {q(schema)}.{q(table)}')
        pcur.execute(f'CREATE TABLE {q(schema)}.{q(table)} ({", ".join(col_defs)}{pk_clause})')

        if with_fk:
            scur.execute(f'PRAGMA foreign_key_list("{table}")')
            for fk in scur.fetchall():
                # (id, seq, table, from, to, on_update, on_delete, match)
                _, _, ref_table, from_col, to_col, *_ = fk
                fk_statements.append(
                    f'ALTER TABLE {q(schema)}.{q(table)} ADD FOREIGN KEY ({q(from_col)}) '
                    f'REFERENCES {q(schema)}.{q(ref_table)} ({q(to_col)})'
                )

        # bulk load via COPY (fast path)
        col_names = [c[1] for c in cols]
        scur.execute(f'SELECT {", ".join(q(c) for c in col_names)} FROM "{table}"')
        buf = io.StringIO()
        writer = csv.writer(buf)
        n = 0
        for row in scur:
            writer.writerow(["\\N" if v is None else v for v in row])
            n += 1
        buf.seek(0)
        if n:
            pcur.copy_expert(
                f'COPY "{schema}"."{table}" FROM STDIN WITH (FORMAT csv, NULL \'\\N\')', buf
            )
        print(f"    {table}: {n} rows")

    if with_fk:
        for stmt in fk_statements:
            try:
                pcur.execute(stmt)
            except Exception as e:
                print(f"    [warn] FK skipped ({e})")
                pconn.rollback()
                pcur = pconn.cursor()  # reset after rollback
            else:
                pconn.commit()

    pconn.commit()
    sconn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dir", default="dataset")
    ap.add_argument("--database-url", required=True,
                     help="postgresql://user:pass@host:port/spider")
    ap.add_argument("--only", default=None, help="load a single db_id")
    ap.add_argument("--recreate", action="store_true",
                     help="drop and recreate the schema first (default: create-if-missing, tables always dropped+recreated)")
    ap.add_argument("--with-fk", action="store_true",
                     help="also create foreign key constraints (best-effort, non-fatal on failure)")
    args = ap.parse_args()

    dataset_dir = Path(args.dataset_dir)
    db_root = dataset_dir / "database"
    db_ids = sorted(p.name for p in db_root.iterdir() if p.is_dir())
    if args.only:
        db_ids = [d for d in db_ids if d == args.only]
        if not db_ids:
            sys.exit(f"db_id '{args.only}' not found under {db_root}")

    pconn = psycopg2.connect(args.database_url)
    pconn.autocommit = False

    for i, db_id in enumerate(db_ids, 1):
        sqlite_path = db_root / db_id / f"{db_id}.sqlite"
        if not sqlite_path.exists():
            print(f"[{i}/{len(db_ids)}] [skip] {db_id}: no .sqlite file at {sqlite_path}")
            continue
        print(f"[{i}/{len(db_ids)}] {db_id}")
        try:
            load_one_db(pconn, db_id, sqlite_path, args.recreate, args.with_fk)
        except Exception as e:
            pconn.rollback()
            print(f"  [error] {db_id}: {e}")

    pconn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
