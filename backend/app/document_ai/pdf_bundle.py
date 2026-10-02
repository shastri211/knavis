from dataclasses import dataclass
from pathlib import Path
import re
import fitz

@dataclass
class LogicalDocument:
    id: str
    source_file: str
    start_page: int
    end_page: int
    confidence: float
    reason: str

def page_texts(pdf_path: Path):
    with fitz.open(pdf_path) as doc:
        return [
            (i + 1, page.get_text("text").strip())
            for i, page in enumerate(doc)
        ]

def likely_boundary(previous: str, current: str) -> tuple[bool, str]:
    """
    Conservative heuristic used only to propose boundaries.
    It does NOT claim perfect document segmentation.

    Strong signals:
    - explicit cover/title-like page
    - form/document keywords
    - long blank transition
    """
    cur = current.strip()
    if not cur:
        return False, ""

    first_lines = [x.strip() for x in cur.splitlines() if x.strip()][:8]
    head = " ".join(first_lines).lower()

    markers = (
        "passport", "visa", "bank statement", "application form",
        "invoice", "certificate", "identity card", "government of",
        "statement of account", "purchase order", "agreement",
    )
    if any(m in head for m in markers):
        return True, "new document-type marker on page"

    if len(first_lines) <= 3 and len(cur) < 500:
        if re.search(r"\b(agreement|certificate|invoice|statement|application)\b", head):
            return True, "short title-like page"

    return False, ""

def segment_pdf(pdf_path: Path) -> list[LogicalDocument]:
    pages = page_texts(pdf_path)
    if not pages:
        return []

    docs = []
    start = pages[0][0]
    reason = "start of PDF"

    for idx in range(1, len(pages)):
        prev_page, prev_text = pages[idx - 1]
        page, text = pages[idx]
        boundary, why = likely_boundary(prev_text, text)
        if boundary:
            docs.append(LogicalDocument(
                id=f"{pdf_path.name}:doc{len(docs)+1}",
                source_file=pdf_path.name,
                start_page=start,
                end_page=prev_page,
                confidence=0.65,
                reason=reason,
            ))
            start = page
            reason = why

    docs.append(LogicalDocument(
        id=f"{pdf_path.name}:doc{len(docs)+1}",
        source_file=pdf_path.name,
        start_page=start,
        end_page=pages[-1][0],
        confidence=0.65,
        reason=reason,
    ))
    return docs
