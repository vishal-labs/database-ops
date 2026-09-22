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


def get_column_types(database_url, db_id):
    """{lowercased_column_name: postgres_data_type} for every column in this schema.
    Used to detect SQLite-vs-Postgres typing mismatches (see rewrite_types below).
    NOTE: keyed by column name only -- if a column name is reused with a different
    type across tables in the same schema (a real Spider quirk, e.g. a foreign key
    declared TEXT on one side and INTEGER on the other), only one type survives
    here. For that case see get_column_types_by_table + rewrite_column_join_types,
    which are table-aware."""
    if db_id in _type_map_cache:
        return _type_map_cache[db_id]
    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s",
            (db_id,),
        )
        type_map = {name.lower(): dtype for name, dtype in cur.fetchall()}
    finally:
        conn.close()
    _type_map_cache[db_id] = type_map
    return type_map


_table_type_cache = {}


def get_column_types_by_table(database_url, db_id):
    """{(table_lower, column_lower): postgres_data_type}, table-aware."""
    if db_id in _table_type_cache:
        return _table_type_cache[db_id]
    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s",
            (db_id,),
        )
        table_types = {(t.lower(), c.lower()): dtype for t, c, dtype in cur.fetchall()}
    finally:
        conn.close()
    _table_type_cache[db_id] = table_types
    return table_types


_TEXT_TYPES = {"text", "character varying", "character", "char", "varchar", "bpchar", "citext"}
_NUMERIC_TYPES = {"bigint", "integer", "smallint", "numeric", "double precision", "real",
                   "smallserial", "serial", "bigserial"}
_NUM_RE = r"-?\d+(?:\.\d+)?"


def _type_class(dtype):
    if dtype in _TEXT_TYPES:
        return "text"
    if dtype in _NUMERIC_TYPES:
        return "numeric"
    return "other"


def rewrite_types(sql, type_map):
    """
    SQLite has no real type enforcement, so Spider's gold SQL routinely compares a
    TEXT-affinity column to a bare numeric literal (e.g. "Year" = 2014) -- SQLite
    coerces this silently, Postgres raises 'operator does not exist: text = integer'.
    This quotes bare numeric literals that are directly compared (=, <>, !=, <, >,
    <=, >=, IN (...)) against a column we know is actually TEXT in Postgres, so the
    comparison becomes text = text instead. Only touches comparisons against known
    text-typed columns; leaves numeric-column comparisons untouched.
    """
    if not type_map:
        return sql

    def is_text_col(name):
        return type_map.get(name.lower()) in _TEXT_TYPES

    # "Col" OP number
    def fix_col_op_num(m):
        col, op, num = m.group("col"), m.group("op"), m.group("num")
        if is_text_col(col):
            return f'"{col}" {op} \'{num}\''
        return m.group(0)

    sql = re.sub(
        rf'"(?P<col>[^"]+)"\s*(?P<op>=|<>|!=|<=|>=|<|>)\s*(?P<num>{_NUM_RE})\b',
        fix_col_op_num, sql,
    )

    # number OP "Col"  (reversed order)
    def fix_num_op_col(m):
        num, op, col = m.group("num"), m.group("op"), m.group("col")
        if is_text_col(col):
            return f"'{num}' {op} \"{col}\""
        return m.group(0)

    sql = re.sub(
        rf'(?P<num>{_NUM_RE})\s*(?P<op>=|<>|!=|<=|>=|<|>)\s*"(?P<col>[^"]+)"',
        fix_num_op_col, sql,
    )

    # "Col" [NOT] IN (n1, n2, ...)
    def fix_in_list(m):
        col, items = m.group("col"), m.group("items")
        if not is_text_col(col):
            return m.group(0)
        parts = [p.strip() for p in items.split(",")]
        fixed = [f"'{p}'" if re.fullmatch(_NUM_RE, p) else p for p in parts]
        return f'"{col}" {m.group("kw")}IN ({", ".join(fixed)})'

    sql = re.sub(
        rf'"(?P<col>[^"]+)"\s+(?P<kw>(?:NOT\s+)?)IN\s*\((?P<items>[^()]*)\)',
        fix_in_list, sql, flags=re.IGNORECASE,
    )

    return sql


_FROM_JOIN_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+"?(?P<table>[A-Za-z_][A-Za-z0-9_]*)"?'
    r'(?:\s+(?:AS\s+)?"?(?P<alias>[A-Za-z_][A-Za-z0-9_]*)"?)?',
    re.IGNORECASE,
)
_NOT_AN_ALIAS = {
    "where", "on", "group", "order", "having", "limit", "join", "inner", "left",
    "right", "full", "union", "intersect", "except", "select", "using", "natural",
    "cross", "as", "by",
}


def _alias_map(sql):
    """{alias_lower: real_table_lower} for every FROM/JOIN in this query, including
    an identity entry for a table used without an alias (so "table"."col" and
    alias."col" resolve the same way)."""
    amap = {}
    for m in _FROM_JOIN_RE.finditer(sql):
        table = m.group("table").lower()
        amap[table] = table
        alias = m.group("alias")
        if alias and alias.lower() not in _NOT_AN_ALIAS:
            amap[alias.lower()] = table
    return amap


_COL_EQ_RE = re.compile(
    r'(?:(?P<lalias>[A-Za-z_][A-Za-z0-9_]*)\.)?"(?P<lcol>[^"]+)"'
    r'\s*(?P<op>=|<>|!=)\s*'
    r'(?:(?P<ralias>[A-Za-z_][A-Za-z0-9_]*)\.)?"(?P<rcol>[^"]+)"'
)


def rewrite_column_join_types(sql, table_types):
    """
    Spider's SQLite schemas sometimes declare a foreign-key pair with different
    types on each side (e.g. concert.Stadium_ID as TEXT, stadium.Stadium_ID as
    INTEGER) -- SQLite's loose typing tolerates joining/comparing them directly,
    Postgres raises 'operator does not exist: text = bigint'. This resolves each
    side of a column = column / <> comparison back to its real table via the
    query's own FROM/JOIN aliases, and casts both sides to ::text ONLY when their
    actual stored types genuinely differ in class (text vs numeric) -- same-type
    comparisons (including two numeric or two text columns) are left untouched,
    so this never changes the meaning of a query that was already correct.
    """
    if not table_types:
        return sql
    amap = _alias_map(sql)

    def resolve_type(alias, col):
        table = amap.get((alias or "").lower())
        if table:
            return table_types.get((table, col.lower()))
        return None

    def fix(m):
        ltype = resolve_type(m.group("lalias"), m.group("lcol"))
        rtype = resolve_type(m.group("ralias"), m.group("rcol"))
        if ltype and rtype and _type_class(ltype) != _type_class(rtype) \
                and "other" not in (_type_class(ltype), _type_class(rtype)):
            lhs = (f'{m.group("lalias")}.' if m.group("lalias") else "") + f'"{m.group("lcol")}"'
            rhs = (f'{m.group("ralias")}.' if m.group("ralias") else "") + f'"{m.group("rcol")}"'
            return f'({lhs})::text {m.group("op")} ({rhs})::text'
        return m.group(0)

    return _COL_EQ_RE.sub(fix, sql)


def rewrite_gold_sql(sql, case_map, type_map, table_types=None):
    """
    Full fix-up for Spider gold SQL run against a Postgres schema loaded from the
    matching SQLite database: rewrite bare identifiers to their real stored case
    (see rewrite_case), quote bare numeric literals compared against columns that
    are actually TEXT (see rewrite_types), and cast mismatched-type FK columns in
    join conditions to a common type (see rewrite_column_join_types). String
    literals are protected from all three passes throughout.
    """
    parts = re.split(r"('(?:[^']|'')*')", sql)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # a string literal -- leave untouched
            out.append(part)
        else:
            fixed = rewrite_case(part, case_map)
            fixed = rewrite_types(fixed, type_map)
            if table_types:
                fixed = rewrite_column_join_types(fixed, table_types)
            out.append(fixed)
    return "".join(out)


def is_order_sensitive(gold_sql):
    return bool(re.search(r"\border\s+by\b", gold_sql, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Case-folding fix for databases loaded BEFORE load_spider_to_postgres.py's
# lowercasing fix (or any schema whose table/column names have mixed case).
# Spider's gold SQL is written assuming SQLite's case-insensitive matching
# ("name" resolves to a column stored as "Name"); Postgres does not do this
# for quoted identifiers, so bare lowercase references fail against a
# mixed-case schema. Rather than reloading the database, we look up the real
# stored names for a schema and rewrite gold SQL to reference them correctly,
# leaving string literals untouched.
# ---------------------------------------------------------------------------

_case_map_cache = {}
_type_map_cache = {}
_ident_pattern_cache = {}


def get_case_map(database_url, db_id):
    """{lowercased_name: actual_stored_name} for every table/column in this schema."""
    if db_id in _case_map_cache:
        return _case_map_cache[db_id]
    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (db_id,),
        )
        names = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s",
            (db_id,),
        )
        names |= {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    case_map = {n.lower(): n for n in names}
    _case_map_cache[db_id] = case_map
    return case_map


def rewrite_case(sql, case_map):
    """
    Rewrite bare identifiers in `sql` that case-insensitively match a known
    table/column name to their actual stored (quoted) case. Content inside
    single-quoted string literals is left untouched. If a name is already
    lowercase in the schema, this is a no-op for it -- safe to always apply,
    including against schemas already loaded with the lowercasing fix.
    """
    if not case_map:
        return sql

    key = tuple(sorted(case_map))
    pattern = _ident_pattern_cache.get(key)
    if pattern is None:
        alts = sorted(case_map.keys(), key=len, reverse=True)
        # (?<!") / (?!") skip identifiers that are already double-quoted in the
        # source SQL (e.g. gold SQL that already writes "Year" correctly) --
        # otherwise we'd re-wrap them into a broken '""Year""'.
        pattern = re.compile(r'(?<!")\b(' + "|".join(re.escape(a) for a in alts) + r')\b(?!")',
                              re.IGNORECASE)
        _ident_pattern_cache[key] = pattern

    def replace(m):
        actual = case_map[m.group(0).lower()]
        return actual if actual.islower() else '"' + actual + '"'

    # split on single-quoted string literals ('' inside one is an escaped quote)
    # so we never touch the contents of a literal value.
    parts = re.split(r"('(?:[^']|'')*')", sql)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # a string literal -- leave untouched
            out.append(part)
        else:
            out.append(pattern.sub(replace, part))
    return "".join(out)