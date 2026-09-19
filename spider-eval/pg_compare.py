"""
Execution-accuracy comparator for the Postgres/MCP path: each Spider db_id lives in
its own Postgres SCHEMA (see load_spider_to_postgres.py). We execute both the gold
SQL and the predicted SQL ourselves, on a fresh read-only connection, with
search_path pinned to that schema -- this is deliberately independent of whatever
search_path the agent's own MCP tool calls used, so a scoring run is never affected
by MCP connection pooling/session-reuse quirks. It mirrors server.js's own
`execTx(sql, writable=false)` -> BEGIN READ ONLY pattern for the safety property
(a broken predicted query can never write to the spider database).
"""
import re
from collections import Counter

import psycopg2


def execute(database_url, db_id, sql, timeout=15):
    """Returns (rows, error). rows is None on failure. Read-only, schema-scoped, timeout-bounded."""
    if not sql or not sql.strip():
        return None, "empty query"
    conn = None
    try:
        conn = psycopg2.connect(database_url)
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {int(timeout * 1000)}")
        cur.execute('SET search_path TO %s, public', (db_id,))
        cur.execute(sql)
        rows = cur.fetchall()
        return rows, None
    except Exception as e:
        return None, str(e)
    finally:
        if conn is not None:
            conn.close()


def _normalize_cell(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, str):
        return v.strip().lower()
    return v


def _normalize_row(row, ignore_col_order):
    vals = [_normalize_cell(v) for v in row]
    if ignore_col_order:
        vals = sorted(vals, key=repr)
    return tuple(vals)


def rows_match(pred_rows, gold_rows, order_sensitive, ignore_col_order=True):
    if pred_rows is None or gold_rows is None:
        return False
    if order_sensitive:
        p = [_normalize_row(r, ignore_col_order) for r in pred_rows]
        g = [_normalize_row(r, ignore_col_order) for r in gold_rows]
        return p == g
    p = Counter(_normalize_row(r, ignore_col_order) for r in pred_rows)
    g = Counter(_normalize_row(r, ignore_col_order) for r in gold_rows)
    return p == g


def is_order_sensitive(gold_sql):
    return bool(re.search(r"\border\s+by\b", gold_sql, re.IGNORECASE))
