"""Table helpers shared by extractors and the chunker."""
import re

_WS_RE = re.compile(r"\s+")


def clean_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return _WS_RE.sub(" ", str(value)).strip()


def clean_rows_numbered(rows, numbers=None) -> tuple[list[list[str]], list[int]]:
    """Normalise cells to strings and drop fully empty rows and trailing empty columns.

    Returns the rows together with each row's ORIGINAL 1-based number (``numbers`` if given),
    so a citation can point at the row a person would find in the spreadsheet.
    """
    numbers = list(numbers) if numbers is not None else list(range(1, len(rows or []) + 1))
    kept, kept_numbers = [], []
    for number, row in zip(numbers, rows or []):
        cells = [clean_cell(c) for c in row]
        if any(cells):
            kept.append(cells)
            kept_numbers.append(number)
    if not kept:
        return [], []
    width = max(len(r) for r in kept)
    while width > 1 and not any(len(r) >= width and r[width - 1] for r in kept):
        width -= 1
    return [r[:width] + [""] * (width - len(r)) for r in kept], kept_numbers


def clean_rows(rows) -> list[list[str]]:
    return clean_rows_numbered(rows)[0]


def render_rows(rows: list[list[str]]) -> str:
    """Pipe-separated text, one row per line (header first)."""
    return "\n".join(" | ".join(row) for row in rows)


def split_rows(rows: list[list[str]], max_chars: int, header_rows: int = 1) -> list[tuple[int, int, list[list[str]]]]:
    """Split a table into row groups of at most ``max_chars``, repeating the header in each.

    Returns ``(first_row, last_row, rows_with_header)``, 1-based row numbers counted in the
    original table (the header is row 1). A single row longer than the limit is kept whole.
    """
    if not rows:
        return []
    header = rows[:header_rows]
    body = rows[header_rows:]
    header_len = len(render_rows(header)) + 1 if header else 0
    if not body:
        return [(1, len(rows), rows)]

    groups, current, start, size = [], [], header_rows + 1, header_len
    for index, row in enumerate(body, header_rows + 1):
        row_len = len(render_rows([row])) + 1
        if current and size + row_len > max_chars:
            groups.append((start, index - 1, header + current))
            current, start, size = [], index, header_len
        current.append(row)
        size += row_len
    if current:
        groups.append((start, header_rows + len(body), header + current))
    return groups
