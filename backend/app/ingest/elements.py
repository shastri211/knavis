from dataclasses import asdict, dataclass, field
from typing import Any

# Element kinds. Text-like kinds can be merged into chunks; the others stay whole.
HEADING = "heading"          # section title; sets the breadcrumb for what follows
PARAGRAPH = "paragraph"      # native text block
OCR_TEXT = "ocr_text"        # text recognised from an image or scanned page
RECORD = "record"            # flattened JSON/XML lines
TRANSCRIPT = "transcript"    # timed speech or subtitle cue
TABLE = "table"              # rows (header first); split by rows, header repeated
SLIDE = "slide"              # one presentation slide (title, body, notes)
SUMMARY = "summary"          # generated description of a sheet/table (columns, size)
FIGURE = "figure"            # description of a chart/graph/diagram (vision, later phase)

TEXT_LIKE = frozenset({PARAGRAPH, OCR_TEXT, RECORD})


class ExtractionError(Exception):
    """A file could not be read. The message is written for the user and is safe to show."""


@dataclass
class Element:
    """One structural unit of a document, with its provenance.

    ``id`` and ``group`` are relative to the document (``p3:e2``). They are prefixed with the
    document id when stored, so a cached extraction can be reused by a different document.
    """
    id: str
    kind: str
    text: str
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    locator: str | None = None          # human-readable position: "page 3", "slide 2", "rows 40-80"
    level: int | None = None            # heading depth, 1 = top
    bbox: list | None = None
    source: str = "native"              # native | ocr | vision | asr
    confidence: float | None = None
    group: str | None = None            # chunks never merge across groups (e.g. PDF bundle parts)
    rows: list[list[str]] | None = None  # table cells, header row first
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, {}, [])} | {"id": self.id, "kind": self.kind, "text": self.text}

    @classmethod
    def from_dict(cls, data: dict) -> "Element":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ExtractionResult:
    kind: str                                    # detected format family (pdf, docx, image, ...)
    elements: list[Element] = field(default_factory=list)
    # What is known without any paid call: page counts, pages that need OCR, figure counts...
    info: dict[str, Any] = field(default_factory=dict)
    # Spreadsheets only: the sheets as tables, read in the same pass for the analytics table store. Never cached.
    tables: list = field(default_factory=list)


def format_clock(seconds: float) -> str:
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
