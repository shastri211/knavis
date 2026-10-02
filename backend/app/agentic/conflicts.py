from dataclasses import dataclass
import re

from ..retrieval.text import STOPWORDS, stem

# A number followed by a unit word or percent sign, e.g. "90 days", "12%", "5 USD".
_QUANTITY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(%|[^\W\d_]{2,})")

@dataclass
class Conflict:
    topic: str
    evidence_ids: list[int]
    reason: str

def _normalize_number(raw: str) -> str:
    value = raw.replace(",", "")
    return value.rstrip("0").rstrip(".") if "." in value else value

def detect_numeric_conflicts(evidence: list[dict], evidence_ids: list[int] | None = None) -> list[Conflict]:
    """
    Flag quantities that DIFFERENT documents report with the same unit but different
    values (e.g. "30 days" in one file, "90 days" in another).

    Different numbers inside one document are normal ("90 days retention, 30 days backups")
    and are not conflicts. This is a flag for the caller to surface, never a proof, and it
    never blocks an answer on its own. ``evidence_ids`` limits the check to cited items.
    """
    wanted = set(evidence_ids) if evidence_ids else None
    by_unit: dict[str, dict[str, dict[str, set[int]]]] = {}
    for idx, item in enumerate(evidence, 1):
        if wanted is not None and idx not in wanted:
            continue
        md = item.get("metadata") or item.get("payload") or {}
        doc = str(md.get("document_id") or md.get("source") or idx)
        text = item.get("text") or md.get("text") or ""
        for number, unit in _QUANTITY_RE.findall(text):
            unit = unit.lower()
            if unit in STOPWORDS:
                continue
            by_unit.setdefault(stem(unit), {}).setdefault(doc, {}).setdefault(_normalize_number(number), set()).add(idx)

    conflicts = []
    for unit, docs in by_unit.items():
        if len(docs) < 2:
            continue
        value_sets = [set(values) for values in docs.values()]
        union = set().union(*value_sets)
        if len(union) > 1 and not set.intersection(*value_sets):
            ids = sorted({i for values in docs.values() for found in values.values() for i in found})
            conflicts.append(Conflict(
                topic=f"quantity:{unit}",
                evidence_ids=ids,
                reason=f"Different documents give different values for '{unit}': {', '.join(sorted(union)[:6])}",
            ))
    return conflicts
