"""Spreadsheet sheets and CSV files as real, queryable tables.

Each chat session owns one small SQLite file (``<data_dir>/tables/<session>.sqlite``) holding one table per
sheet. A catalog row per table (``DataTable`` in the application database) records its columns, their
types, the values of low-cardinality columns and the real sheet row numbers, which is what the SQL writer
sees and what a citation points at. Keeping each session's data in its own file means a query can only ever
reach that session's tables, and deleting the session is deleting the file.
"""
import logging
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..ingest.extractors.tabular import SheetTable
from ..models import DataTable, Document

logger = logging.getLogger("mragrag")

ROW_COLUMN = "_row"          # the row's number in the original sheet
MAX_COLUMNS = 200
MAX_DISTINCT_LISTED = 12     # a column with at most this many distinct values has them listed for the SQL writer
SAMPLE_ROWS = 3

_NULL_TOKENS = frozenset({"", "n/a", "na", "#n/a", "null", "none", "nan", "-", "—"})
_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_LEADING_ZERO_RE = re.compile(r"^[+-]?0\d")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_CURRENCY = "$₹€£¥"


def tables_dir() -> Path:
    return settings.data_dir / "tables"


def table_file(session_id: str) -> Path:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "", session_id)
    return tables_dir() / f"{safe}.sqlite"


# ---- typing --------------------------------------------------------------------------------

def parse_number(value: str):
    """An int or float for numeric-looking text ("1,234", "$5.50", "-3"), else ``None``. Codes like "007" are not numbers."""
    text = value.strip()
    if text.isdigit() and text.isascii() and (len(text) == 1 or text[0] != "0") and len(text) < 19:
        return int(text)   # fast path for the common case
    if text[:1] in _CURRENCY and text[:1]:
        text = text[1:].strip()
    if _THOUSANDS_RE.match(text):
        text = text.replace(",", "")
    if _LEADING_ZERO_RE.match(text) and "." not in text:
        return None   # an identifier or zip code, not a quantity
    if _INT_RE.match(text):
        number = int(text)
        return number if abs(number) < 2 ** 63 else None
    if _FLOAT_RE.match(text):
        return float(text)
    return None


def infer_column(values: list[str]):
    """``(kind, converted values)`` where kind is integer | number | boolean | date | text | empty."""
    present = [v for v in values if v.strip().lower() not in _NULL_TOKENS]
    if not present:
        return "empty", [None] * len(values)
    lowered = {v.strip().lower() for v in present}
    if lowered <= {"true", "false"}:
        return "boolean", [None if v.strip().lower() in _NULL_TOKENS else int(v.strip().lower() == "true") for v in values]
    parsed = {}
    for v in present:
        if v not in parsed:
            number = parse_number(v)
            if number is None:
                break   # the column is not numeric: stop at the first cell that says so
            parsed[v] = number
    else:
        as_int = all(isinstance(n, int) for n in parsed.values())
        return ("integer" if as_int else "number"), [
            None if v.strip().lower() in _NULL_TOKENS else (parsed[v] if as_int else float(parsed[v])) for v in values]
    if all(_DATE_RE.match(v.strip()) for v in present):
        return "date", [v.strip() if v.strip() else None for v in values]
    return "text", [v if v.strip() else None for v in values]


_SQL_TYPE = {"integer": "INTEGER", "boolean": "INTEGER", "number": "REAL", "date": "TEXT", "text": "TEXT", "empty": "TEXT"}


# ---- naming --------------------------------------------------------------------------------

def _identifier(name: str, fallback: str) -> str:
    """Letters, digits and underscores (any script; Indic vowel signs are kept), no leading digit."""
    cleaned = "".join(
        ch if (ch.isalnum() or ch == "_" or unicodedata.category(ch) in ("Mn", "Mc")) else "_" for ch in name.strip()
    )
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return fallback
    return f"c_{cleaned}" if cleaned[0].isdigit() else cleaned


def column_names(header: list[str]) -> list[str]:
    names, seen = [], {ROW_COLUMN}
    for index, original in enumerate(header[:MAX_COLUMNS], 1):
        base = _identifier(original, f"column_{index}")
        name, n = base, 1
        while name.lower() in seen:
            n += 1
            name = f"{base}_{n}"
        seen.add(name.lower())
        names.append(name)
    return names


def _table_name(filename: str, sheet: str | None, sheet_count: int, taken: set[str]) -> str:
    stem = Path(filename).stem
    parts = [stem] + ([sheet] if sheet and sheet_count > 1 else [])
    base = re.sub(r"[^0-9A-Za-z]+", "_", "_".join(parts)).strip("_").lower()[:60] or "table"
    if base[0].isdigit():
        base = f"t_{base}"
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name


# ---- writing -------------------------------------------------------------------------------

def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    return connection


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:   # e.g. a query still has it open on Windows; the next delete or restart clears it
        logger.warning("Could not remove %s: %s", path.name, exc)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _describe_column(name: str, original: str, kind: str, values: list) -> dict:
    present = [v for v in values if v is not None]
    info = {"name": name, "original": original, "kind": kind, "type": _SQL_TYPE[kind], "nulls": len(values) - len(present)}
    if kind in ("integer", "number", "date") and present:
        info["min"], info["max"] = min(present), max(present)
    counts = Counter(present)
    if kind in ("text", "integer", "boolean") and 0 < len(counts) <= MAX_DISTINCT_LISTED:
        info["values"] = [str(v)[:40] for v, _ in counts.most_common()]
    return info


def replace_document_tables(db: Session, document: Document, sheets: list[SheetTable]) -> list[DataTable]:
    """Load a document's sheets as tables, replacing any it had before. The caller commits."""
    drop_document_tables(db, document.id)
    if not sheets:
        return []
    taken = {name for (name,) in db.query(DataTable.table_name).filter(DataTable.session_id == document.session_id)}
    path = table_file(document.session_id)
    entries, connection = [], _connect(path)
    try:
        for sheet in sheets:
            names = column_names(sheet.header)
            width = len(names)
            columns, converted = [], []
            for index, name in enumerate(names):
                kind, values = infer_column([row[index] if index < len(row) else "" for row in sheet.rows])
                converted.append(values)
                columns.append(_describe_column(name, sheet.header[index], kind, values))
            table = _table_name(document.filename, sheet.sheet, len(sheets), taken)
            taken.add(table)
            definition = ", ".join([f"{_quote(ROW_COLUMN)} INTEGER"] + [f"{_quote(c['name'])} {c['type']}" for c in columns])
            connection.execute(f"DROP TABLE IF EXISTS {_quote(table)}")
            connection.execute(f"CREATE TABLE {_quote(table)} ({definition})")
            connection.executemany(
                f"INSERT INTO {_quote(table)} VALUES ({', '.join('?' * (width + 1))})",
                ([number] + [column[i] for column in converted] for i, number in enumerate(sheet.numbers)),
            )
            entry = DataTable(
                session_id=document.session_id, document_id=document.id, filename=document.filename, sheet=sheet.sheet,
                table_name=table, columns_json=columns, row_count=len(sheet.rows),
                sample_json=[row[:width] for row in sheet.rows[:SAMPLE_ROWS]],
                first_row=sheet.numbers[0], last_row=sheet.numbers[-1], truncated=sheet.truncated,
            )
            db.add(entry)
            entries.append(entry)
        connection.commit()
    finally:
        connection.close()
    db.flush()
    return entries


def drop_document_tables(db: Session, document_id: str) -> None:
    """Remove a document's tables from its session's table file and from the catalog. The caller commits."""
    entries = db.query(DataTable).filter(DataTable.document_id == document_id).all()
    if not entries:
        return
    path = table_file(entries[0].session_id)
    if path.exists():
        connection = _connect(path)
        try:
            for entry in entries:
                connection.execute(f"DROP TABLE IF EXISTS {_quote(entry.table_name)}")
            connection.commit()
            remaining = connection.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            connection.close()
        if not remaining:
            _remove_file(path)
    for entry in entries:
        db.delete(entry)
    db.flush()


def drop_session_tables(db: Session, session_id: str) -> None:
    """Remove every table of a session: the file and the catalog rows. The caller commits."""
    _remove_file(table_file(session_id))
    db.query(DataTable).filter(DataTable.session_id == session_id).delete()


def session_tables(db: Session, session_id: str) -> list[DataTable]:
    return db.query(DataTable).filter(DataTable.session_id == session_id).order_by(DataTable.created_at, DataTable.table_name).all()
