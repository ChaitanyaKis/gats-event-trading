"""Defensive field helpers for exchange payloads, whose field names and
formats drift over time."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from typing import Any

_EMPTY = frozenset({"", "-", "null", "none", "nan", "na", "n/a"})


def pick(row: Mapping[str, Any], *keys: str) -> Any:
    """First non-empty value among ``keys``, matched case-insensitively."""
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value).strip().lower() not in _EMPTY:
            return value
    return None


def clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return None if text.lower() in _EMPTY else text


def to_float(value: Any) -> float | None:
    text = clean_str(value)
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    if number is None:
        return None
    return round(number)


def decode_text(payload: bytes) -> str:
    """Decode exchange text files; they are UTF-8 (sometimes with BOM) or Latin-1."""
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return payload.decode("latin-1")


def read_csv(payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Parse CSV with stripped header names and values."""
    reader = csv.reader(io.StringIO(decode_text(payload)))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return [], []
    header = [h.strip().upper() for h in rows[0]]
    records = [
        {header[i]: cell.strip() for i, cell in enumerate(row) if i < len(header)}
        for row in rows[1:]
    ]
    return header, records


def preview(payload: bytes, limit: int = 120) -> str:
    """Short, log-safe view of a payload for error messages."""
    text = payload[:limit].decode("utf-8", errors="replace")
    return " ".join(text.split()) + ("..." if len(payload) > limit else "")


def looks_like_html(payload: bytes) -> bool:
    head = payload[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")
