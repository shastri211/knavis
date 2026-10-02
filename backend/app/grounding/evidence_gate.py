from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..retrieval.text import lexical_coverage

@dataclass
class EvidenceDecision:
    sufficient: bool
    reason: str
    selected: list[dict[str, Any]]

def _text_of(item: dict[str, Any]) -> str:
    return (item.get("text") or (item.get("payload") or {}).get("text") or "").strip()

def assess_evidence(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    min_dense: float | None = None,
    min_coverage: float | None = None,
    max_items: int | None = None,
    min_items: int = 1,
) -> EvidenceDecision:
    """Decide whether retrieved candidates can support an answer.

    Each retrieval signal is judged on its own scale instead of comparing one raw number
    to a single cutoff (BM25 scores are corpus-relative and can be negative on small
    corpora, cosine similarities are bounded). A candidate is usable when ANY holds:

    * its dense cosine similarity reaches ``min_dense`` (works across languages);
    * it covers at least ``min_coverage`` of the question's content terms (lexical);
    * it was selected as a whole-document overview sample.
    """
    min_dense = settings.evidence_min_dense if min_dense is None else min_dense
    min_coverage = settings.evidence_min_coverage if min_coverage is None else min_coverage
    max_items = settings.evidence_max_items if max_items is None else max_items

    usable = []
    for item in candidates:
        text = _text_of(item)
        if not text:
            continue
        dense = item.get("dense_score")
        coverage, matched, terms = lexical_coverage(query, text)
        dense_ok = dense is not None and float(dense) >= min_dense
        lexical_ok = terms > 0 and matched > 0 and coverage >= min_coverage
        if item.get("overview") or dense_ok or lexical_ok:
            usable.append({**item, "lexical_coverage": round(coverage, 3)})

    if not any(item.get("overview") for item in usable):
        usable.sort(key=lambda item: item.get("rrf_score", 0.0), reverse=True)
    usable = usable[:max_items]

    if len(usable) < min_items:
        return EvidenceDecision(
            False,
            "Retrieved evidence does not meet the configured sufficiency threshold.",
            usable,
        )
    return EvidenceDecision(True, "Sufficient candidate evidence.", usable)


def build_evidence_packet(selected: list[dict[str, Any]]) -> str:
    blocks = []
    for i, item in enumerate(selected, 1):
        metadata = item.get("metadata") or item.get("payload") or {}
        source = metadata.get("source") or item.get("source_id") or "unknown source"
        page = metadata.get("page")
        location = f"{source}, page {page}" if page else str(source)
        blocks.append(
            f"[EVIDENCE {i} | {location}]\n"
            f"{(item.get('text') or metadata.get('text') or '').strip()}"
        )
    return "\n\n".join(blocks)
