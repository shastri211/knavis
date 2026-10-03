"""Spreadsheet sheets and CSV files as real, queryable tables.

Each spreadsheet document owns one small SQLite file holding one table per sheet. The file is stored through
``app.storage`` (the data directory, or an object store shared by several hosts) under a key that is new on every
write (``tables/<session>/<document>-<token>.sqlite``), so a copy cached on any host's disk is never stale. A catalog
row per table (``DataTable`` in the application database) records the file's reference, the table's columns, their
types, the values of low-cardinality columns and the real sheet row numbers, which is what the SQL writer sees and what
a citation points at.

A question is answered from one local file that holds every table of the chat: a chat with a single spreadsheet uses its
file directly, several are merged into a derived "view" file (cheap, rebuilt whenever the set of files changes). Each
chat's data sits in files of its own, so a query can only ever reach that chat's tables, and deleting the chat is
deleting its files.

Databases from before object storage kept one file per chat (``<data dir>/tables/<session>.sqlite``);
``migrate_legacy_table_files`` splits those into per-document files once, at start-up.
"""
import logging
import re
import shutil
import sqlite3
import time
import unicodedata
from collections import Counter
from hashlib import sha1
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from .. import storage
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


def _safe(identifier: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_-]", "", identifier)


def session_prefix(session_id: str) -> str:
    """The storage key prefix holding every table file of a chat."""
    return f"tables/{_safe(session_id)}/"


def legacy_table_file(session_id: str) -> Path:
    """The single per-chat file used before table files were per document."""
    return tables_dir() / f"{_safe(session_id)}.sqlite"


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


def _delete_ref(ref: str) -> None:
    """Remove a table file from storage and from this host's cache."""
    storage.delete(ref)
    if storage.is_object(ref):
        _remove_file(settings.data_dir / ref[len(storage.S3_SCHEME):])


def replace_document_tables(db: Session, document: Document, sheets: list[SheetTable]) -> list[DataTable]:
    """Load a document's sheets as tables in a new file, replacing any it had before. The caller commits."""
    previous = {e.file_key for e in db.query(DataTable).filter(DataTable.document_id == document.id) if e.file_key}
    drop_document_tables(db, document.id, remove_files=False)
    if not sheets:
        for ref in previous:
            _delete_ref(ref)
        return []
    taken = {name for (name,) in db.query(DataTable.table_name).filter(DataTable.session_id == document.session_id)}
    key = f"{session_prefix(document.session_id)}{_safe(document.id)}-{uuid4().hex[:12]}.sqlite"
    path = settings.data_dir / key
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
            connection.execute(f"CREATE TABLE {_quote(table)} ({definition})")
            connection.executemany(
                f"INSERT INTO {_quote(table)} VALUES ({', '.join('?' * (width + 1))})",
                ([number] + [column[i] for column in converted] for i, number in enumerate(sheet.numbers)),
            )
            entries.append(DataTable(
                session_id=document.session_id, document_id=document.id, filename=document.filename, sheet=sheet.sheet,
                table_name=table, columns_json=columns, row_count=len(sheet.rows),
                sample_json=[row[:width] for row in sheet.rows[:SAMPLE_ROWS]],
                first_row=sheet.numbers[0], last_row=sheet.numbers[-1], truncated=sheet.truncated,
            ))
        connection.commit()
        connection.close()
        ref = storage.publish(key, path)   # object storage: upload now; the local file stays as this host's cache
    except BaseException:
        connection.close()
        _remove_file(path)
        raise
    for entry in entries:
        entry.file_key = ref
        db.add(entry)
    db.flush()
    for old in previous:   # only once the new rows are in: a failure above keeps the old file the catalog still points at
        _delete_ref(old)
    return entries


def drop_document_tables(db: Session, document_id: str, *, remove_files: bool = True) -> None:
    """Remove a document's tables from storage and from the catalog. The caller commits."""
    entries = db.query(DataTable).filter(DataTable.document_id == document_id).all()
    if not entries:
        return
    if remove_files:
        for ref in {e.file_key for e in entries if e.file_key}:
            _delete_ref(ref)
    legacy = [e for e in entries if not e.file_key]
    if legacy:
        _drop_from_legacy_file(legacy[0].session_id, [e.table_name for e in legacy])
    for entry in entries:
        db.delete(entry)
    db.flush()


def _drop_from_legacy_file(session_id: str, table_names: list[str]) -> None:
    """Tables of a chat that still sit in the old per-chat file (the start-up migration has not moved them)."""
    path = legacy_table_file(session_id)
    if not path.exists():
        return
    connection = _connect(path)
    try:
        for name in table_names:
            connection.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
        connection.commit()
        remaining = connection.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    finally:
        connection.close()
    if not remaining:
        _remove_file(path)


def drop_session_tables(db: Session, session_id: str) -> None:
    """Remove every table of a chat: its files (including strays) and the catalog rows. The caller commits."""
    for ref in {e.file_key for e in db.query(DataTable).filter(DataTable.session_id == session_id) if e.file_key}:
        _delete_ref(ref)
    storage.delete_prefix(session_prefix(session_id))        # files whose rows never committed
    shutil.rmtree(settings.data_dir / session_prefix(session_id).rstrip("/"), ignore_errors=True)   # this host's cache
    _remove_file(legacy_table_file(session_id))
    db.query(DataTable).filter(DataTable.session_id == session_id).delete()


# ---- reading -------------------------------------------------------------------------------

_VIEW_BATCH = 8            # SQLite allows 10 attached databases by default
_VIEW_KEEP_SECONDS = 600


def query_file(session_id: str) -> Path | None:
    """A local SQLite file holding every table of the chat, or ``None`` when it has none.

    The files are looked up from the catalog by chat, never taken from the caller, so a query cannot be pointed at another
    chat's data. Files that live in object storage are downloaded into this host's cache the first time they are needed.
    """
    from ..db import SessionLocal
    with SessionLocal() as db:
        refs = sorted({ref for (ref,) in db.query(DataTable.file_key).filter(
            DataTable.session_id == session_id, DataTable.file_key.isnot(None))})
    if not refs:
        return None
    try:
        paths = [storage.ensure_local(ref) for ref in refs]
    except FileNotFoundError:
        return None
    if len(paths) == 1:
        return paths[0]
    folder = settings.data_dir / session_prefix(session_id).rstrip("/")
    view = folder / f"view-{sha1('|'.join(refs).encode()).hexdigest()[:16]}.sqlite"
    if not view.exists():
        _build_view(view, paths)
        now = time.time()
        for stale in folder.glob("view-*.sqlite"):
            if stale != view and now - stale.stat().st_mtime > _VIEW_KEEP_SECONDS:
                _remove_file(stale)
    return view


def _build_view(view: Path, sources: list[Path]) -> None:
    partial = view.with_name(f".{view.name}.{uuid4().hex[:8]}.part")
    connection = _connect(partial)
    try:
        for start in range(0, len(sources), _VIEW_BATCH):
            batch = sources[start:start + _VIEW_BATCH]
            for number, source in enumerate(batch):
                connection.execute(f"ATTACH DATABASE ? AS src{number}", (str(source.resolve()),))
            for number in range(len(batch)):
                schema = f"src{number}"
                for name, ddl in connection.execute(
                        f"SELECT name, sql FROM {schema}.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
                    connection.execute(ddl)
                    connection.execute(f"INSERT INTO {_quote(name)} SELECT * FROM {schema}.{_quote(name)}")
            connection.commit()
            for number in range(len(batch)):
                connection.execute(f"DETACH DATABASE src{number}")
        connection.close()
        partial.replace(view)
    except BaseException:
        connection.close()
        _remove_file(partial)
        raise


def migrate_legacy_table_files() -> int:
    """Split old per-chat table files into per-document files (once, at start-up). Returns the documents moved."""
    from ..db import SessionLocal
    moved = 0
    with SessionLocal() as db:
        by_session: dict[str, list[DataTable]] = {}
        for entry in db.query(DataTable).filter(DataTable.file_key.is_(None)).all():
            by_session.setdefault(entry.session_id, []).append(entry)
        for session_id, entries in by_session.items():
            source = legacy_table_file(session_id)
            if not source.exists():
                continue   # nothing to move; these tables stay unqueryable until the document is processed again
            by_document: dict[str, list[DataTable]] = {}
            for entry in entries:
                by_document.setdefault(entry.document_id, []).append(entry)
            for document_id, rows in by_document.items():
                key = f"{session_prefix(session_id)}{_safe(document_id)}-{uuid4().hex[:12]}.sqlite"
                target = settings.data_dir / key
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    keep = {r.table_name for r in rows}
                    connection = _connect(target)
                    try:
                        for (name,) in connection.execute(
                                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
                            if name not in keep:
                                connection.execute(f"DROP TABLE {_quote(name)}")
                        connection.commit()
                        connection.execute("VACUUM")
                    finally:
                        connection.close()
                    ref = storage.publish(key, target)
                except Exception as exc:
                    logger.warning("Could not move the tables of document %s to their own file: %s: %s",
                                   document_id, type(exc).__name__, str(exc)[:120])
                    _remove_file(target)
                    continue
                for row in rows:
                    row.file_key = ref
                db.commit()
                moved += 1
            if not db.query(DataTable.id).filter(DataTable.session_id == session_id, DataTable.file_key.is_(None)).first():
                _remove_file(source)
    if moved:
        logger.info("Moved the spreadsheet tables of %s document(s) to per-document files", moved)
    return moved


def sweep_table_cache(max_age_seconds: float = 3600) -> int:
    """Delete cached copies of table files that no catalog row refers to any more (object storage only: with the local
    backend these files *are* the storage). Files younger than ``max_age_seconds`` are left alone: they may belong to an
    ingestion that has not committed yet."""
    if storage.backend_name() != "s3" or not tables_dir().exists():
        return 0
    from ..db import SessionLocal
    with SessionLocal() as db:
        referenced = {ref for (ref,) in db.query(DataTable.file_key).filter(DataTable.file_key.isnot(None))}
    now, removed = time.time(), 0
    for path in tables_dir().rglob("*.sqlite"):
        if path.name.startswith("view-") or path.parent == tables_dir():
            continue   # views are derived; the legacy layout is not a cache
        ref = storage.S3_SCHEME + path.relative_to(settings.data_dir).as_posix()
        if ref not in referenced and now - path.stat().st_mtime > max_age_seconds:
            _remove_file(path)
            removed += 1
    return removed


def session_tables(db: Session, session_id: str) -> list[DataTable]:
    return db.query(DataTable).filter(DataTable.session_id == session_id).order_by(DataTable.created_at, DataTable.table_name).all()
