from dataclasses import dataclass

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
    It verifies that the answer contains valid evidence markers. A later phase
    can add a dedicated entailment/verification model after benchmarking.
    """
    import re
    refs = [int(x) for x in re.findall(r"\[EVIDENCE\s+(\d+)", answer)]
    valid = [x for x in refs if 1 <= x <= len(evidence)]
    if refs and len(valid) == len(refs):
        return ClaimCheck(True, answer, valid, "All cited evidence IDs exist.")
    return ClaimCheck(False, answer, valid, "Missing or invalid evidence references.")
