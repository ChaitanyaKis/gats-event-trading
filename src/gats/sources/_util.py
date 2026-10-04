"""Defensive field helpers for exchange payloads, whose field names and
formats drift over time."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree

from gats.sources.models import PayloadError

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


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_ZIP_MAGIC = b"PK\x03\x04"


def is_workbook(payload: bytes) -> bool:
    """Does the payload look like an Excel workbook (a zip container)?"""
    return payload[:4] == _ZIP_MAGIC


def _column(reference: str) -> int:
    """Zero-based column of a cell reference: ``A1`` -> 0, ``AB7`` -> 27."""
    letters = re.match(r"[A-Z]+", reference)
    if letters is None:
        raise PayloadError(f"workbook: bad cell reference {reference!r}")
    index = 0
    for letter in letters.group():
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def workbook_rows(payload: bytes) -> list[list[str]]:
    """The first sheet of an ``.xlsx`` workbook as rows of text.

    NSE has served a workbook under a ``.csv`` address (verified 2026-10-04:
    ``sec_bhavdata_full_08082022.csv`` is one, with the usual columns). The
    format is a zip of XML, so the standard library reads it: text cells
    point into a shared-strings table, numbers are written in place.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
            sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
            if not sheets:
                raise PayloadError("workbook: no worksheet inside")
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                table = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in table.iter(f"{_SHEET_NS}si"):
                    shared.append("".join(t.text or "" for t in item.iter(f"{_SHEET_NS}t")))
            sheet = ElementTree.fromstring(archive.read(sheets[0]))
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
        raise PayloadError(f"workbook: cannot be read ({exc})") from exc
    rows: list[list[str]] = []
    for row in sheet.iter(f"{_SHEET_NS}row"):
        cells: dict[int, str] = {}
        for cell in row.iter(f"{_SHEET_NS}c"):
            value = cell.find(f"{_SHEET_NS}v")
            kind = cell.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{_SHEET_NS}t"))
            elif value is None or value.text is None:
                continue
            elif kind == "s":
                try:
                    text = shared[int(value.text)]
                except (ValueError, IndexError) as exc:
                    raise PayloadError(f"workbook: bad shared string {value.text!r}") from exc
            else:
                text = value.text
            cells[_column(cell.get("r") or "")] = text
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


def read_csv(payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Parse CSV (or a workbook served in its place) with stripped header
    names and values. Anything unreadable is a :class:`PayloadError`, never
    a crash: one odd file must not stop a backfill of thousands."""
    if is_workbook(payload):
        rows = [row for row in workbook_rows(payload) if any(cell.strip() for cell in row)]
    else:
        try:
            # newline="": the csv module then handles stray line breaks itself.
            reader = csv.reader(io.StringIO(decode_text(payload), newline=""))
            rows = [row for row in reader if any(cell.strip() for cell in row)]
        except csv.Error as exc:
            raise PayloadError(
                f"not readable as CSV ({exc}); first bytes {payload[:40]!r}"
            ) from exc
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
