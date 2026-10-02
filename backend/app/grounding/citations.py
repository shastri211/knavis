import re

# Matches [EVIDENCE 1], [EVIDENCE 1 | file, page 2], [EVIDENCE 1, EVIDENCE 3], [evidence 2].
_MARKER_RE = re.compile(r"\[\s*EVIDENCE\s+([^\]|]*?)(?:\|[^\]]*)?\]", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")

# Kept for callers that import the old name.
CITATION_RE = _MARKER_RE


def extract_citation_ids(text: str) -> list[int]:
    """All evidence numbers cited in ``text``, in order of appearance (duplicates kept)."""
    ids: list[int] = []
    for match in _MARKER_RE.finditer(text or ""):
        ids.extend(int(n) for n in _DIGITS_RE.findall(match.group(1)))
    return ids


def strip_citations(text: str) -> str:
    return _MARKER_RE.sub("", text or "")


def validate_citations(answer: str, evidence: list[dict]) -> tuple[bool, list[int]]:
    refs = extract_citation_ids(answer)
    if not refs:
        # The finalizer can require citations for knowledge answers.
        return False, []
    valid = [x for x in refs if 1 <= x <= len(evidence)]
    return len(valid) == len(refs), valid


def source_citations(evidence: list[dict], ids: list[int] | None = None) -> list[dict]:
    """Citation records for ``evidence``.

    ``ids`` are the 1-based evidence numbers shown to the model; ``evidence_id`` in the
    output keeps that numbering so it always matches the ``[EVIDENCE N]`` in the answer.
    """
    ids = ids or list(range(1, len(evidence) + 1))
    out = []
    for i in ids:
        item = evidence[i - 1]
        md = item.get("metadata") or item.get("payload") or {}
        out.append({
            "evidence_id": i,
            "source": md.get("source") or item.get("source_id"),
            "page": md.get("page"),
            "locator": md.get("locator"),
            "section": md.get("section"),
            "slide": md.get("slide"),
            "sheet": md.get("sheet"),
            "logical_document_id": md.get("logical_document_id"),
            "chunk_id": md.get("chunk_id") or item.get("id"),
        })
    return out
