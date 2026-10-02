import re
from .types import Claim, VerificationResult
from .conflicts import detect_numeric_conflicts

def extract_claims(answer: str) -> list[Claim]:
    claims = []
    # Sentence-level extraction is intentionally conservative.
    for sentence in re.split(r"(?<=[.!?])\s+", answer.strip()):
        refs = [int(x) for x in re.findall(r"\[EVIDENCE\s+(\d+)", sentence)]
        if sentence.strip():
            claims.append(Claim(sentence.strip(), refs))
    return claims

def verify_answer(answer: str, evidence: list[dict]) -> VerificationResult:
    claims = extract_claims(answer)
    unsupported = [
        c.text for c in claims
        if not c.evidence_ids or any(i < 1 or i > len(evidence) for i in c.evidence_ids)
    ]
    conflicts = [
        c.reason for c in detect_numeric_conflicts(evidence)
    ]
    supported = not unsupported and not conflicts
    return VerificationResult(
        supported=supported,
        claims=claims,
        unsupported_claims=unsupported,
        conflicts=conflicts,
    )
