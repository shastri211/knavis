import re
from collections import Counter
from pathlib import Path

import fitz

from ..elements import HEADING, PARAGRAPH, TABLE, Element, ExtractionError, ExtractionResult
from ..tables import clean_rows, render_rows

# A page with fewer native characters than this (and images, or nothing at all) is treated
# as scanned and queued for OCR instead of being searched as empty text.
OCR_MIN_CHARS = 50
# Images covering at least this share of a page are reported as figures (charts, photos,
# diagrams) so a later vision step can describe them once.
FIGURE_MIN_PAGE_SHARE = 0.05

_BOUNDARY_RE = re.compile(r"(?im)^(?:document|agreement|invoice|statement|passport|visa|application|form)\b")


def _clean(text: str) -> str:
    text = text.replace("­", "")
    text = re.sub(r"-\n(?=\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _overlap_share(box, other) -> float:
    inter = fitz.Rect(box) & fitz.Rect(other)
    area = fitz.Rect(box).get_area()
    return (inter.get_area() / area) if area and not inter.is_empty else 0.0


def _page_blocks(page, table_boxes):
    """Text blocks with font size and boldness, skipping text that belongs to a table."""
    blocks = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        spans = [s for line in block.get("lines", []) for s in line.get("spans", []) if s.get("text", "").strip()]
        text = _clean(" ".join(line_text(line) for line in block.get("lines", [])))
        if not text or any(_overlap_share(block["bbox"], t) > 0.5 for t in table_boxes):
            continue
        size = max((s.get("size", 0) for s in spans), default=0)
        bold = bool(spans) and all(s.get("flags", 0) & 16 for s in spans)
        blocks.append({"text": text, "bbox": [round(v, 1) for v in block["bbox"]], "size": round(size, 1), "bold": bold})
    return blocks


def line_text(line) -> str:
    return "".join(s.get("text", "") for s in line.get("spans", []))


def _logical_segments(page_texts: list[str]) -> list[list[int]]:
    """Heuristic split of a combined PDF into logical documents (a new part starts at a
    title-like line such as "Invoice ..."). Recorded as a hint, never as certain."""
    segments, current = [], []
    for number, _ in enumerate(page_texts, 1):
        current.append(number)
        if number < len(page_texts) and len(current) > 1 and _BOUNDARY_RE.search(page_texts[number].strip()):
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def extract_pdf(path: Path) -> ExtractionResult:
    try:
        pdf = fitz.open(path)
    except Exception as exc:
        raise ExtractionError("This PDF could not be opened; it may be corrupted.") from exc
    with pdf:
        if pdf.needs_pass:
            raise ExtractionError("This PDF is password protected. Remove the password and upload it again.")

        pages = []
        for number, page in enumerate(pdf, 1):
            tables = []
            try:
                for table in page.find_tables().tables:
                    rows = clean_rows(table.extract())
                    if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                        tables.append({"rows": rows, "bbox": [round(v, 1) for v in table.bbox]})
            except Exception:
                pass  # table detection is best-effort; the text is still extracted below
            blocks = _page_blocks(page, [t["bbox"] for t in tables])

            page_area = page.rect.get_area() or 1.0
            figures = 0
            try:
                for info in page.get_image_info():
                    box = fitz.Rect(info["bbox"])
                    if box.get_area() / page_area >= FIGURE_MIN_PAGE_SHARE:
                        figures += 1
            except Exception:
                pass
            chars = sum(len(b["text"]) for b in blocks) + sum(len(render_rows(t["rows"])) for t in tables)
            pages.append({"number": number, "blocks": blocks, "tables": tables, "chars": chars, "figures": figures})

        # Body text is the most common font size; clearly larger or bold short lines are headings.
        sizes = Counter()
        for page in pages:
            for block in page["blocks"]:
                sizes[block["size"]] += len(block["text"])
        body_size = sizes.most_common(1)[0][0] if sizes else 0
        heading_sizes = sorted({b["size"] for p in pages for b in p["blocks"] if body_size and b["size"] >= body_size * 1.15}, reverse=True)

        def heading_level(block):
            text = block["text"]
            if len(text) > 150 or text.endswith((".", ",", ";")) or not body_size:
                return None
            if block["size"] >= body_size * 1.15:
                return min(heading_sizes.index(block["size"]) + 1, 4)
            if block["bold"] and block["size"] >= body_size and len(text) <= 80 and len(text.split()) <= 12:
                return 4
            return None

        page_texts = [" ".join(b["text"] for b in p["blocks"]) for p in pages]
        segments = _logical_segments(page_texts)
        segment_of = {number: index for index, seg in enumerate(segments, 1) for number in seg}

        elements, ocr_pages, figure_pages = [], [], []
        for page in pages:
            number = page["number"]
            group = f"logical:{segment_of.get(number, 1)}"
            # Scanned = (almost) no native text but a page-sized image. A truly blank page has
            # no image and costs nothing: it is never sent to OCR.
            if page["chars"] < OCR_MIN_CHARS and page["figures"]:
                ocr_pages.append(number)
            if page["figures"]:
                figure_pages.append({"page": number, "count": page["figures"]})
            position = 0
            for block in page["blocks"]:
                level = heading_level(block)
                elements.append(Element(
                    id=f"p{number}:e{position}", kind=HEADING if level else PARAGRAPH, text=block["text"],
                    page=number, locator=f"page {number}", level=level, bbox=block["bbox"], group=group,
                ))
                position += 1
            for table in page["tables"]:
                elements.append(Element(
                    id=f"p{number}:e{position}", kind=TABLE, text=render_rows(table["rows"]), rows=table["rows"],
                    page=number, locator=f"page {number}", bbox=table["bbox"], group=group,
                ))
                position += 1

        info = {
            "pages": len(pages),
            "ocr_pages": ocr_pages,
            "estimated_ocr_calls": len(ocr_pages),
            "figure_pages": figure_pages,
            "tables": sum(len(p["tables"]) for p in pages),
            "logical_documents": len(segments),
        }
        return ExtractionResult("pdf", elements, info)
