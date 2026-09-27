#!/usr/bin/env python3
"""Inspect a detached decrypted WeChat snapshot without printing user content.

The report is intended for connector development: it inventories SQLite
tables/columns and aggregates XML tag/attribute names. Values are deliberately
omitted, so the tool can be used to understand a new WeChat build safely.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET


def xml_names(value: object, sample_limit: int = 5000) -> tuple[Counter[str], Counter[str]]:
    tags: Counter[str] = Counter()
    attrs: Counter[str] = Counter()
    if not isinstance(value, (str, bytes, bytearray)):
        return tags, attrs
    try:
        root = ET.fromstring(value)
    except (ET.ParseError, ValueError, TypeError):
        return tags, attrs
    for node in root.iter():
        tags[node.tag.rsplit("}", 1)[-1]] += 1
        attrs.update(str(key).rsplit("}", 1)[-1] for key in node.attrib)
    return tags, attrs


def inspect_db(path: Path, sample_limit: int) -> dict:
    result = {"path": str(path), "tables": []}
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            try:
                columns = [row[1] for row in con.execute(f'PRAGMA table_info("{name.replace(chr(34), chr(34)*2)}")')]
            except sqlite3.DatabaseError:
                result["tables"].append({"name": name, "row_count": None, "columns": [], "xml_columns": [], "error": "schema_unavailable"})
                continue
            try:
                count = int(con.execute(f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34)*2)}"').fetchone()[0])
            except sqlite3.DatabaseError:
                count = None
            tags: Counter[str] = Counter()
            attrs: Counter[str] = Counter()
            xml_columns = []
            for column in columns:
                try:
                    escaped_column = column.replace(chr(34), chr(34)*2)
                    escaped_name = name.replace(chr(34), chr(34)*2)
                    values = con.execute(f'SELECT CAST("{escaped_column}" AS BLOB) FROM "{escaped_name}" WHERE "{escaped_column}" IS NOT NULL LIMIT ?', (sample_limit,))
                except sqlite3.DatabaseError:
                    continue
                local_tags: Counter[str] = Counter()
                local_attrs: Counter[str] = Counter()
                for row in values:
                    t, a = xml_names(row[0])
                    local_tags.update(t)
                    local_attrs.update(a)
                if local_tags or local_attrs:
                    xml_columns.append({"column": column, "tags": dict(local_tags.most_common()), "attributes": dict(local_attrs.most_common())})
            result["tables"].append({"name": name, "row_count": count, "columns": columns, "xml_columns": xml_columns})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--sample-limit", type=int, default=5000)
    args = parser.parse_args()
    if args.sample_limit < 1:
        parser.error("--sample-limit must be positive")
    dbs = sorted(args.snapshot.rglob("*.db"))
    report = {"snapshot": str(args.snapshot.resolve()), "sample_limit": args.sample_limit, "databases": [inspect_db(path, args.sample_limit) for path in dbs]}
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
