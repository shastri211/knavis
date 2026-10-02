from dataclasses import dataclass
from typing import Any

@dataclass
class EvidenceDecision:
    sufficient: bool
    reason: str
    selected: list[dict[str, Any]]

def assess_evidence(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    min_score: float = 0.35,
    min_items: int = 1,
) -> EvidenceDecision:
    usable = []
    for item in candidates:
        score = float(
            item.get("rerank_score",
            item.get("score",
            item.get("rrf_score", 0.0)))
        )
        text = (item.get("text") or item.get("payload", {}).get("text") or "").strip()
        if text and score >= min_score:
            usable.append({**item, "evidence_score": score})

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
