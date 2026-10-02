"""Text-family formats: plain text, Markdown, JSON, XML, HTML, subtitles, e-mail."""
import json
import re
import xml.etree.ElementTree as ET
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from ..elements import (
    HEADING, PARAGRAPH, RECORD, TABLE, TRANSCRIPT, Element, ExtractionError, ExtractionResult, format_clock,
)
from ..tables import clean_rows, render_rows
from .tabular import decode_bytes

RECORD_CHARS = 1200
MAX_XML_BYTES = 20_000_000
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def read_text(path: Path) -> str:
    return decode_bytes(path.read_bytes())


def paragraph_elements(text: str, start: int = 0, **kwargs) -> list[Element]:
    """Blank-line separated blocks as paragraph elements."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        block = block.strip()
        if block:
            out.append(Element(id=f"e{start + len(out)}", kind=PARAGRAPH, text=block, **kwargs))
    return out


def extract_text(path: Path) -> ExtractionResult:
    return ExtractionResult("text", paragraph_elements(read_text(path)))


_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _md_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_elements(text: str, id_prefix: str = "e", **common) -> list[Element]:
    """Markdown as elements: headings, pipe tables (kept as tables), fenced code and paragraphs.

    Used for .md files and for OCR output (Mistral returns markdown with tables). ``common`` is
    applied to every element (page, source, locator, ...).
    """
    elements: list[Element] = []
    buffer: list[str] = []
    table: list[str] = []
    in_fence = False

    def new_id() -> str:
        return f"{id_prefix}{len(elements)}"

    def flush_text():
        block = "\n".join(buffer).strip()
        buffer.clear()
        for part in re.split(r"\n\s*\n", block) if block else []:
            if part.strip():
                elements.append(Element(id=new_id(), kind=PARAGRAPH, text=part.strip(), **common))

    def flush_table():
        lines, table[:] = list(table), []
        rows = [_md_cells(l) for l in lines if not _MD_TABLE_SEP_RE.match(l)]
        rows = clean_rows(rows)
        if len(rows) >= 2:
            elements.append(Element(id=new_id(), kind=TABLE, text=render_rows(rows), rows=rows, **common))
        elif lines:   # a lone pipe line is just text
            buffer.extend(lines)

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE_RE.match(line):
            flush_table()
            in_fence = not in_fence
            buffer.append(line)
            continue
        if not in_fence and line.strip().startswith("|") and line.count("|") >= 2:
            flush_text()
            table.append(line)
            continue
        flush_table()
        heading = None if in_fence else _MD_HEADING_RE.match(line)
        if heading:
            flush_text()
            elements.append(Element(id=new_id(), kind=HEADING, text=heading.group(2).strip(), level=len(heading.group(1)), **common))
        elif not line.strip() and not in_fence:
            flush_text()
        else:
            buffer.append(line)
    flush_table()
    flush_text()
    return elements


def extract_markdown(path: Path) -> ExtractionResult:
    return ExtractionResult("markdown", markdown_elements(read_text(path)))


# ---- structured data ---------------------------------------------------------------------

def _scalar(value) -> str:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, bool) else str(value)


def flatten(value, prefix: str = ""):
    """Yield ``path: value`` lines so every line is understandable on its own."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten(item, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        if value and all(not isinstance(v, (dict, list)) for v in value):
            yield f"{prefix}: " + ", ".join(_scalar(v) for v in value)
        else:
            for index, item in enumerate(value):
                yield from flatten(item, f"{prefix}[{index}]")
    elif value is not None:
        yield f"{prefix or 'value'}: {_scalar(value)}"


def _record_elements(lines, kind_prefix: str) -> list[Element]:
    elements, group, size = [], [], 0

    def emit():
        if group:
            first = group[0].split(":", 1)[0]
            elements.append(Element(id=f"{kind_prefix}{len(elements)}", kind=RECORD, text="\n".join(group), locator=first))
            group.clear()

    for line in lines:
        if group and size + len(line) > RECORD_CHARS:
            emit()
            size = 0
        group.append(line)
        size += len(line) + 1
    emit()
    return elements


def extract_json(path: Path) -> ExtractionResult:
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"This JSON file is not valid (line {exc.lineno}, column {exc.colno}).") from exc
    return ExtractionResult("json", _record_elements(flatten(data), "j"))


def extract_xml(path: Path) -> ExtractionResult:
    raw = path.read_bytes()
    if len(raw) > MAX_XML_BYTES:
        raise ExtractionError("This XML file is too large to index.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ExtractionError("This XML file is not valid.") from exc

    def walk(node, prefix):
        tag = node.tag.rsplit("}", 1)[-1]
        here = f"{prefix}.{tag}" if prefix else tag
        for name, value in node.attrib.items():
            yield f"{here}@{name.rsplit('}', 1)[-1]}: {value}"
        text = (node.text or "").strip()
        if text:
            yield f"{here}: {text}"
        for child in node:
            yield from walk(child, here)

    return ExtractionResult("xml", _record_elements(walk(root, ""), "x"))


# ---- HTML --------------------------------------------------------------------------------

class _Html(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}
    BLOCK = {"p", "div", "section", "article", "li", "ul", "ol", "blockquote", "pre", "header", "footer", "main", "br", "dd", "dt"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.title = ""
        self._skip = 0
        self._text: list[str] = []
        self._heading: int | None = None
        self._in_title = False
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._text)).strip()
        self._text.clear()
        if not text:
            return
        kind, level = (HEADING, self._heading) if self._heading else (PARAGRAPH, None)
        self.elements.append(Element(id=f"e{len(self.elements)}", kind=kind, text=text, level=level))

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.HEADINGS:
            self._flush()
            self._heading = int(tag[1])
        elif tag == "table":
            self._flush()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in self.HEADINGS:
            self._flush()
            self._heading = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            rows = clean_rows(self._table)
            if rows:
                self.elements.append(Element(id=f"e{len(self.elements)}", kind=TABLE, text=render_rows(rows), rows=rows))
            self._table = None
        elif tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        elif self._cell is not None:
            self._cell.append(data)
        else:
            self._text.append(data)


def html_elements(source: str) -> tuple[list[Element], str]:
    parser = _Html()
    parser.feed(source)
    parser.close()
    parser._flush()
    return parser.elements, parser.title.strip()


def extract_html(path: Path) -> ExtractionResult:
    elements, title = html_elements(read_text(path))
    return ExtractionResult("html", elements, {"title": title} if title else {})


# ---- subtitles ---------------------------------------------------------------------------

_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE_RE = re.compile(rf"{_TIME}\s*-->\s*{_TIME}")
_TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def _seconds(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def extract_subtitles(path: Path) -> ExtractionResult:
    elements = []
    for block in re.split(r"\n\s*\n", read_text(path).replace("\r\n", "\n")):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        for index, line in enumerate(lines):
            match = _CUE_RE.search(line)
            if match:
                start, end = _seconds(*match.group(1, 2, 3, 4)), _seconds(*match.group(5, 6, 7, 8))
                text = " ".join(_TAG_RE.sub("", l).strip() for l in lines[index + 1:]).strip()
                if text:
                    elements.append(Element(
                        id=f"c{len(elements)}", kind=TRANSCRIPT, text=text,
                        locator=f"{format_clock(start)}-{format_clock(end)}", source="native",
                        meta={"start_s": start, "end_s": end},
                    ))
                break
    return ExtractionResult("subtitles", elements, {"cues": len(elements)})


# ---- e-mail ------------------------------------------------------------------------------

def extract_email(path: Path) -> ExtractionResult:
    try:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    except Exception as exc:
        raise ExtractionError("This e-mail file could not be read.") from exc

    header = "\n".join(f"{name}: {message[name]}" for name in ("From", "To", "Cc", "Date", "Subject") if message[name])
    elements = [Element(id="e0", kind=PARAGRAPH, text=header, locator="email headers")] if header else []

    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        if body.get_content_subtype() == "html":
            parsed, _ = html_elements(content)
            for el in parsed:
                el.id = f"e{len(elements)}"
                el.locator = "email body"
                elements.append(el)
        else:
            for el in paragraph_elements(content, start=len(elements), locator="email body"):
                elements.append(el)

    attachments = [part.get_filename() for part in message.iter_attachments() if part.get_filename()]
    if attachments:
        elements.append(Element(
            id=f"e{len(elements)}", kind=PARAGRAPH, locator="email attachments",
            text="Attachments (not indexed): " + ", ".join(attachments),
        ))
    return ExtractionResult("email", elements, {"attachments": attachments})
