import re

CITATION_RE = re.compile(r"\[EVIDENCE\s+(\d+)(?:\s*\|[^\]]*)?\]")

def validate_citations(answer: str, evidence: list[dict]) -> tuple[bool, list[int]]:
    refs = [int(x) for x in CITATION_RE.findall(answer)]
    if not refs:
        # The finalizer can require citations for knowledge answers.
        return False, []
    valid = [x for x in refs if 1 <= x <= len(evidence)]
    return len(valid) == len(refs), valid

def source_citations(evidence: list[dict]) -> list[dict]:
    out = []
    for i, item in enumerate(evidence, 1):
        md = item.get("metadata") or item.get("payload") or {}
        out.append({
            "evidence_id": i,
            "source": md.get("source") or item.get("source_id"),
            "page": md.get("page"),
            "logical_document_id": md.get("logical_document_id"),
        })
    return out
