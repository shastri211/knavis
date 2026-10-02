"""Structure-aware chunking, done once at ingestion.

* Headings are not chunks; they become a "Section: A > B" line on what follows.
* Consecutive small paragraphs merge, but never across a page, slide, sheet or logical
  document, so a chunk's page/slide/sheet provenance is exact.
* Tables are split by rows with the header repeated; slides, summaries and figures stay whole.
* Timed transcripts merge into time windows.
* Only a paragraph longer than the hard maximum is split, on sentence boundaries with overlap.
"""
import re
from dataclasses import dataclass, field

from .elements import (
    FIGURE, HEADING, OCR_TEXT, SLIDE, SUMMARY, TABLE, TEXT_LIKE, TRANSCRIPT, Element, format_clock,
)
from .tables import render_rows, split_rows

TARGET = 1200        # preferred chunk size (characters)
HARD_MAX = 1800      # no chunk exceeds this unless a single table row does
OVERLAP = 150        # only used when one paragraph has to be split
MIN_TAIL = 80        # a smaller trailing chunk is merged into its neighbour
WINDOW_SECONDS = 120  # longest span of speech merged into one transcript chunk

_SENTENCE_RE = re.compile(r"(?<=[.!?।])\s+|\n+")


@dataclass
class ChunkData:
    ordinal: int
    text: str
    kind: str
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    locator: str | None = None
    section: str | None = None
    element_ids: list[str] = field(default_factory=list)
    bbox: list | None = None
    group: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class _Draft:
    kind: str
    parts: list[str]
    page: int | None
    slide: int | None
    sheet: str | None
    locator: str | None
    section: str | None
    element_ids: list[str]
    bbox: list | None
    group: str | None
    meta: dict
    mergeable: bool = True
    sep: str = "\n\n"     # OCR words flow as one line of text; paragraphs are separated by a blank line

    @property
    def size(self) -> int:
        """Final text length, including the "Section: ..." line prepended to the body."""
        return sum(len(p) for p in self.parts) + len(self.sep) * max(0, len(self.parts) - 1) + _prefix_len(self.section, self.kind)


def _prefix_len(section: str | None, kind: str) -> int:
    return len(f"Section: {section}\n") if section and kind not in (SLIDE, SUMMARY) else 0


def split_text(text: str, target: int = TARGET, overlap: int = OVERLAP) -> list[str]:
    """Split on sentence boundaries into pieces of at most ``target`` characters, carrying
    the last ``overlap`` characters of sentences into the next piece."""
    text = text.strip()
    if len(text) <= target:
        return [text] if text else []
    sentences = []
    for sentence in (s.strip() for s in _SENTENCE_RE.split(text)):
        # No punctuation: cut at a space. Walk an index instead of re-slicing the remainder each time, which copied the
        # rest of the text for every piece and made a long unbroken paragraph take quadratic time.
        pos, end = 0, len(sentence)
        while end - pos > target:
            cut = sentence.rfind(" ", pos, pos + target) - pos
            cut = cut if cut > target // 2 else target
            piece = sentence[pos:pos + cut].strip()
            if piece:
                sentences.append(piece)
            pos += cut
            while pos < end and sentence[pos].isspace():
                pos += 1
        tail = sentence[pos:].strip()
        if tail:
            sentences.append(tail)

    pieces, current = [], []
    for sentence in sentences:
        if current and sum(len(s) + 1 for s in current) + len(sentence) > target:
            pieces.append(" ".join(current))
            carried, size = [], 0
            for previous in reversed(current):
                if size + len(previous) > overlap:
                    break
                carried.insert(0, previous)
                size += len(previous) + 1
            current = carried
        current.append(sentence)
    if current:
        pieces.append(" ".join(current))
    return pieces


def build_chunks(elements: list[Element], target: int = TARGET, hard_max: int = HARD_MAX, overlap: int = OVERLAP) -> list[ChunkData]:
    drafts: list[_Draft] = []
    stack: list[tuple[int, str]] = []     # open headings: (level, text)
    current: _Draft | None = None

    def section() -> str | None:
        return " > ".join(text for _, text in stack) or None

    def flush():
        nonlocal current
        if current is not None:
            _commit(current)
        current = None

    def _commit(draft: _Draft):
        body_len = draft.size
        previous = drafts[-1] if drafts else None
        if (previous is not None and draft.mergeable and previous.mergeable and body_len < MIN_TAIL
                and _same_place(previous, draft) and previous.size + len(previous.sep) + body_len <= hard_max):
            previous.parts.extend(draft.parts)
            previous.element_ids.extend(draft.element_ids)
            return
        drafts.append(draft)

    def draft_from(el: Element, kind: str, parts: list[str], mergeable=True, **overrides) -> _Draft:
        values = dict(
            kind=kind, parts=parts, page=el.page, slide=el.slide, sheet=el.sheet, locator=el.locator,
            section=section(), element_ids=[el.id], bbox=el.bbox, group=el.group, meta={}, mergeable=mergeable,
            sep=" " if kind == OCR_TEXT else "\n\n",
        )
        values.update(overrides)
        return _Draft(**values)

    for el in elements:
        text = (el.text or "").strip()
        if el.kind == HEADING:
            flush()
            if text:
                while stack and stack[-1][0] >= (el.level or 1):
                    stack.pop()
                stack.append((el.level or 1, text))
            continue
        if not text and not el.rows:
            continue

        if el.kind in TEXT_LIKE or el.kind not in (TABLE, SLIDE, SUMMARY, FIGURE, TRANSCRIPT):
            kind = el.kind if el.kind in TEXT_LIKE else "paragraph"
            if len(text) + _prefix_len(section(), kind) > hard_max:
                flush()
                for piece in split_text(text, target, overlap):
                    drafts.append(draft_from(el, kind, [piece], mergeable=False))
                continue
            candidate = draft_from(el, kind, [text])
            if current is not None and _same_place(current, candidate) and current.kind == kind and current.size + len(current.sep) + len(text) <= target:
                current.parts.append(text)
                current.element_ids.append(el.id)
            else:
                flush()
                current = candidate
            continue

        if el.kind == TRANSCRIPT:
            start, end = el.meta.get("start_s"), el.meta.get("end_s")
            line = f"{el.meta['speaker']}: {text}" if el.meta.get("speaker") else text
            window_ok = (
                current is not None and current.kind == TRANSCRIPT and start is not None and end is not None
                and current.meta.get("start_s") is not None and end - current.meta["start_s"] <= WINDOW_SECONDS
            )
            if window_ok and _same_place(current, draft_from(el, TRANSCRIPT, [line])) and current.size + len(current.sep) + len(line) <= target:
                current.parts.append(line)
                current.element_ids.append(el.id)
                current.meta["end_s"] = end
                current.locator = f"{format_clock(current.meta['start_s'])}-{format_clock(end)}"
            else:
                flush()
                meta = {k: el.meta[k] for k in ("start_s", "end_s") if k in el.meta}
                current = draft_from(el, TRANSCRIPT, [line], mergeable=False, meta=meta)
            continue

        flush()
        if el.kind == TABLE and el.rows:
            # Spreadsheet extractors pre-group rows and set an exact locator (real sheet row numbers).
            pre_grouped = "row_start" in el.meta
            rendered = render_rows(el.rows)
            if pre_grouped and len(rendered) + _prefix_len(section(), TABLE) <= hard_max:
                drafts.append(draft_from(el, TABLE, [rendered], mergeable=False))
                continue
            for first, last, rows in split_rows(el.rows, target):
                if pre_grouped:   # an oversize group: keep its locator (exact rows are unknown after a split)
                    locator = el.locator
                else:
                    label = f"rows {first}-{last}" if last > first else f"row {first}"
                    locator = f"{el.locator}, {label}" if el.locator else label
                drafts.append(draft_from(el, TABLE, [render_rows(rows)], mergeable=False, locator=locator))
            continue

        # slide, summary, figure (or a table with no row data): keep whole when it fits
        for piece in split_text(text, target, overlap) if len(text) > hard_max else [text]:
            drafts.append(draft_from(el, el.kind, [piece], mergeable=False))
    flush()

    chunks = []
    for ordinal, d in enumerate(drafts):
        body = d.sep.join(d.parts)
        text = f"Section: {d.section}\n{body}" if d.section and d.kind not in (SLIDE, SUMMARY) else body
        chunks.append(ChunkData(
            ordinal=ordinal, text=text, kind=d.kind, page=d.page, slide=d.slide, sheet=d.sheet,
            locator=d.locator or (f"page {d.page}" if d.page else None), section=d.section,
            element_ids=d.element_ids, bbox=d.bbox, group=d.group, meta=d.meta,
        ))
    return chunks


def _same_place(a: _Draft, b: _Draft) -> bool:
    return (a.page, a.slide, a.sheet, a.group, a.section) == (b.page, b.slide, b.sheet, b.group, b.section)
