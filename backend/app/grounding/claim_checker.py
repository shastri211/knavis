from dataclasses import dataclass

from .citations import extract_citation_ids

@dataclass
class ClaimCheck:
    supported: bool
    claim: str
    evidence_ids: list[int]
    reason: str

def basic_claim_check(answer: str, evidence: list[dict]) -> ClaimCheck:
    """
    Conservative second gate.

    This intentionally does not pretend to be a semantic entailment model.
    It verifies that the answer contains valid evidence markers. Per-claim checks
    live in ``app.agentic.verification``.
    """
    refs = extract_citation_ids(answer)
    valid = [x for x in refs if 1 <= x <= len(evidence)]
    if refs and len(valid) == len(refs):
        return ClaimCheck(True, answer, valid, "All cited evidence IDs exist.")
    return ClaimCheck(False, answer, valid, "Missing or invalid evidence references.")
