"""Turn a Spider tables.json entry into a compact schema description for prompting."""
import json
from pathlib import Path


def load_tables(tables_json_path):
    with open(tables_json_path) as f:
        data = json.load(f)
    return {t["db_id"]: t for t in data}


def schema_text(table_entry):
    tables = table_entry["table_names_original"]
    cols = table_entry["column_names_original"]  # [ [table_idx, col_name], ... ], first row is (-1, '*')
    types = table_entry["column_types"]

    pks = set()
    for pk in table_entry.get("primary_keys", []):
        if isinstance(pk, list):
            pks.update(pk)
        else:
            pks.add(pk)

    fks = table_entry.get("foreign_keys", [])

    cols_by_table = {i: [] for i in range(len(tables))}
    for col_idx, (t_idx, col_name) in enumerate(cols):
        if t_idx == -1:
            continue
        col_type = types[col_idx].upper() if col_idx < len(types) else "TEXT"
        marker = " PRIMARY KEY" if col_idx in pks else ""
        cols_by_table[t_idx].append(f"{col_name} {col_type}{marker}")

    lines = [f"Database: {table_entry['db_id']}"]
    for i, tname in enumerate(tables):
        lines.append(f"Table: {tname} ({', '.join(cols_by_table[i])})")

    if fks:
        lines.append("Foreign Keys:")
        for a, b in fks:
            ta, ca = cols[a]
            tb, cb = cols[b]
            lines.append(f"  {tables[ta]}.{ca} -> {tables[tb]}.{cb}")

    return "\n".join(lines)
