from dataclasses import dataclass
import re

@dataclass
class Conflict:
    topic: str
    evidence_ids: list[int]
    reason: str

def detect_numeric_conflicts(evidence: list[dict]) -> list[Conflict]:
    """
    Conservative detector for obviously different numeric values in the
    retrieved evidence. It is a flagging mechanism, not a semantic proof.
    """
    groups = {}
    for idx, item in enumerate(evidence, 1):
        text = item.get("text") or item.get("payload", {}).get("text", "")
        nums = re.findall(r"\b\d+(?:\.\d+)?\b", text)
        for n in nums:
            groups.setdefault(n, []).append(idx)

    # If several distinct numeric values occur, flag rather than choose one.
    values = list(groups)
    if len(values) > 1:
        return [Conflict(
            topic="numeric_values",
            evidence_ids=sorted({i for ids in groups.values() for i in ids}),
            reason=f"Retrieved evidence contains multiple numeric values: {', '.join(values[:8])}",
        )]
    return []
