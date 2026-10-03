"""Strict validation and execution of model-written SQL.

Nothing the model writes is trusted. A query runs only when ALL of these hold:

* it is one statement, starts with SELECT (or WITH), has no comments and is short;
* SQLite itself, through its authorizer, allows only reads of the session's own tables and columns and
  only functions from an allowlist (so no PRAGMA, ATTACH, DDL/DML, recursive CTEs or extensions can run,
  however the statement is spelled);
* the connection is read-only and cut off after a time limit;
* at most ``max_rows`` rows come back (``ANALYTICS_MAX_ROWS``; the time limit is ``ANALYTICS_TIMEOUT_SECONDS``).

The authorizer also reports exactly which columns were read, which becomes the citation.
"""
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from .tablestore import ROW_COLUMN, query_file

MAX_SQL_CHARS = 2000

ALLOWED_FUNCTIONS = frozenset({
    # aggregates
    "count", "sum", "total", "avg", "min", "max", "group_concat",
    # scalar
    "abs", "round", "coalesce", "ifnull", "nullif", "iif", "length", "lower", "upper", "trim", "ltrim", "rtrim",
    "substr", "substring", "replace", "instr", "like", "glob", "typeof",
    # dates (columns of type date are ISO text)
    "date", "time", "datetime", "julianday", "strftime",
    # math (present only when SQLite was built with math functions)
    "sqrt", "floor", "ceil", "ceiling", "pow", "power", "ln", "log", "log10", "exp", "mod", "sign",
})


class SqlRejected(Exception):
    """The query was refused or failed. The message is safe to show to the user."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool                                   # more rows existed than ``max_rows``
    columns_used: dict[str, set[str]] = field(default_factory=dict)   # table -> columns the query read
    seconds: float = 0.0


def _scan(sql: str) -> None:
    """Reject ';' and comments, ignoring text inside quotes."""
    quote = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                if sql[i + 1:i + 2] == quote:   # doubled quote is an escaped quote
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
        elif ch == "[":
            quote = "]"
        elif ch == ";":
            raise SqlRejected("Only a single statement is allowed.")
        elif sql.startswith("--", i) or sql.startswith("/*", i):
            raise SqlRejected("Comments are not allowed.")
        i += 1
    if quote:
        raise SqlRejected("The statement has an unterminated quote.")


_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_QUOTED_RE = re.compile(r'"((?:[^"]|"")*)"')
_ALIAS_AFTER_RE = re.compile(r'\bAS\s+(?:"((?:[^"]|"")*)"|(\w+))', re.IGNORECASE)
_ALIAS_BEFORE_RE = re.compile(r'(?:"((?:[^"]|"")*)"|(\w+))\s+AS\b', re.IGNORECASE)


def _check_quoted_names(sql: str, known: set[str]) -> None:
    """Every "double-quoted name" must be a table, a column or an alias the query itself defines.

    SQLite would otherwise read an unknown one as a string literal, so a misspelt column returns its own
    name as data. Newer SQLite builds can switch that off (see ``run_select``); this check does not depend on
    the version, so the behaviour is the same everywhere.
    """
    aliases = {(a or b).replace('""', '"').lower() for a, b in _ALIAS_AFTER_RE.findall(sql) + _ALIAS_BEFORE_RE.findall(sql)}
    for name in _QUOTED_RE.findall(_LITERAL_RE.sub("''", sql)):
        if name.replace('""', '"').lower() not in known | aliases:
            raise SqlRejected(f'The query uses "{name}", which is not a table or column.')


def clean_sql(sql) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("No query was written.")
    sql = sql.strip().rstrip(";").strip()
    if len(sql) > MAX_SQL_CHARS:
        raise SqlRejected("The query is too long.")
    _scan(sql)
    if not re.match(r"(?is)^(select|with)\b", sql):
        raise SqlRejected("Only SELECT queries are allowed.")
    return sql


def run_select(session_id: str, sql, tables, *, max_rows: int | None = None, timeout: float | None = None) -> QueryResult:
    """Validate and run ``sql`` against the session's tables. ``tables`` are the session's ``DataTable`` rows.

    Raises ``SqlRejected`` for anything that is not a plain, allowed, read-only query or that does not run.
    """
    max_rows = settings.analytics_max_rows if max_rows is None else max_rows
    timeout = settings.analytics_timeout_seconds if timeout is None else timeout
    sql = clean_sql(sql)
    allowed = {   # table -> {lower-case column -> real name}; the real names are what gets cited
        t.table_name.lower(): {c["name"].lower(): c["name"] for c in t.columns_json} | {ROW_COLUMN: ROW_COLUMN} for t in tables
    }
    path = query_file(session_id) if allowed else None
    if not allowed or path is None:
        raise SqlRejected("There are no spreadsheet tables to query in this chat.")
    _check_quoted_names(sql, set(allowed) | {c for cols in allowed.values() for c in cols})

    used: dict[str, set[str]] = {}
    denied: list[str] = []

    def authorizer(action, arg1, arg2, database, _source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table, column = (arg1 or "").lower(), (arg2 or "").lower()
            if table in allowed and (column == "" or column in allowed[table]):
                columns = used.setdefault(table, set())   # COUNT(*) reads the table without naming a column
                if column:
                    columns.add(allowed[table][column])
                return sqlite3.SQLITE_OK
            denied.append(f"{arg1}.{arg2}" if arg2 else str(arg1))
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in ALLOWED_FUNCTIONS:
                return sqlite3.SQLITE_OK
            denied.append(f"function {arg2}")
            return sqlite3.SQLITE_DENY
        denied.append(f"operation {action}")
        return sqlite3.SQLITE_DENY   # PRAGMA, ATTACH, INSERT, CREATE, recursive CTEs, ...

    deadline = time.monotonic() + timeout
    connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only = ON")
        if hasattr(connection, "setconfig"):   # Python 3.12+: make SQLite itself reject unknown "double-quoted names" too
            connection.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        connection.set_authorizer(authorizer)
        connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20_000)
        started = time.monotonic()
        try:
            cursor = connection.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "interrupted" in message:
                raise SqlRejected("The query took too long.") from exc
            if denied:
                raise SqlRejected(f"The query used something that is not allowed ({denied[0]}).") from exc
            raise SqlRejected(f"The query did not run: {message}") from exc
        except sqlite3.DatabaseError as exc:
            raise SqlRejected(f"The query did not run: {exc}") from exc
        if denied:
            raise SqlRejected(f"The query used something that is not allowed ({denied[0]}).")
        if not columns:
            raise SqlRejected("The query returned nothing to show.")
        names = {t.table_name.lower(): t.table_name for t in tables}
        return QueryResult(
            columns=columns, rows=[tuple(r) for r in rows[:max_rows]], truncated=len(rows) > max_rows,
            columns_used={names[t]: cols for t, cols in used.items()}, seconds=time.monotonic() - started,
        )
    finally:
        connection.close()
