"""Trim a real probe payload into a small test fixture.

Usage (from the repo root):
    .venv/Scripts/python scripts/trim_fixture.py data/probes/bse-....json \
        tests/fixtures/real/bse_ann_2026-10-02.json --rows 30

Real samples keep the tests honest about the exchanges' actual formats, but a
full day's file is too big for git. The trim keeps the original bytes of each
kept row (CSV) or the original objects (JSON), so the fixture parses exactly
like the live payload. For CSV, rows are picked to cover every distinct value
of each ``--cover`` column first (e.g. SERIES), then filled in file order.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def trim_json(payload: bytes, rows: int) -> bytes:
    data: Any = json.loads(payload)
    if isinstance(data, list):
        data = data[:rows]
    elif isinstance(data, dict) and isinstance(data.get("Table"), list):
        data = {**data, "Table": data["Table"][:rows]}
    elif isinstance(data, dict) and isinstance(data.get("data"), list):
        data = {**data, "data": data["data"][:rows]}
    else:
        raise SystemExit("unrecognised JSON shape; trim it by hand")
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def trim_csv(payload: bytes, rows: int, cover: list[str]) -> bytes:
    lines = payload.splitlines(keepends=True)
    header, body = lines[0], [line for line in lines[1:] if line.strip()]
    names = [h.strip().upper() for h in header.decode("utf-8-sig").split(",")]
    keep: list[int] = []
    for column in cover:
        col = names.index(column.upper())
        seen: set[str] = set()
        for index, line in enumerate(body):
            cells = next(csv.reader([line.decode("utf-8", errors="replace")]))
            value = cells[col].strip()
            if value not in seen:
                seen.add(value)
                if index not in keep:
                    keep.append(index)
    for index in range(len(body)):
        if len(keep) >= rows:
            break
        if index not in keep:
            keep.append(index)
    return header + b"".join(body[i] for i in sorted(keep[:rows]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--rows", type=int, default=30)
    parser.add_argument(
        "--cover",
        action="append",
        default=[],
        help="CSV column whose distinct values must all appear (repeatable)",
    )
    args = parser.parse_args()
    payload = args.source.read_bytes()
    if args.source.suffix == ".json":
        out = trim_json(payload, args.rows)
    else:
        out = trim_csv(payload, args.rows, args.cover)
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_bytes(out)
    print(f"wrote {args.target} ({len(out):,} bytes)")


if __name__ == "__main__":
    main()
