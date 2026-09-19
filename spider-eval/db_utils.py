"""Execute SQL against a Spider SQLite database and compare result sets (execution accuracy)."""
import re
import sqlite3
import threading
from collections import Counter


def _run(db_path, sql, timeout):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    cur = conn.cursor()
    timer = threading.Timer(timeout, conn.interrupt)
    timer.start()
    try:
        cur.execute(sql)
        return cur.fetchall()
    finally:
        timer.cancel()
        conn.close()


def execute(db_path, sql, timeout=15):
    """Returns (rows, error). rows is None on failure."""
    if not sql or not sql.strip():
        return None, "empty query"
    try:
        rows = _run(str(db_path), sql, timeout)
        return rows, None
    except Exception as e:
        return None, str(e)


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
    """
    Execution-accuracy comparison.
    - order_sensitive: rows must appear in the same sequence (used when gold has ORDER BY)
    - ignore_col_order: sort values within a row so SELECT column permutations still match
    NOTE: this is a practical approximation of Spider's official execution-match scorer,
    not a byte-for-byte reimplementation of taoyds/spider's evaluation.py.
    """
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
