import re
import unicodedata

from ..grounding.citations import extract_citation_ids, strip_citations
from ..retrieval.text import content_terms, tokenize
from .types import Claim, VerificationResult
from .conflicts import detect_numeric_conflicts

# Sentences that talk ABOUT the answer rather than assert document facts. They need no citation.
_META_RE = re.compile(
    r"let me know|hope (this|that) helps|feel free|happy to help|"
    r"(could not|couldn't|cannot|can't|do not|don't|did not|didn't) (find|see|locate)|"
    r"(not|n't) (available|mentioned|provided|stated|specified|included|present|found)\b|"
    r"based on the (provided |supplied |given )?(evidence|documents?|context|material)|"
    r"according to the (provided |supplied |given )?(evidence|documents?|context|material)|"
    r"insufficient (evidence|information)|मुझे बताएं|मुझे बताइए|batayein|bataiye",
    re.IGNORECASE,
)
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Minimum share of a claim's (Latin-script) content terms that must appear in the evidence
# it cites. Low on purpose: this catches unrelated or invented statements, not paraphrases.
_MIN_TERM_SUPPORT = 0.3
# Cited evidence with fewer Latin-script content terms than this is treated as non-English.
_MIN_LATIN_EVIDENCE_TERMS = 3

def _numbers(text: str) -> set[str]:
    """Numbers in ``text`` with thousands separators removed and non-ASCII digits normalized."""
    ascii_text = "".join(str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch for ch in text)
    out = set()
    for raw in _NUMBER_RE.findall(ascii_text):
        value = raw.replace(",", "").rstrip(",")
        out.add(value.rstrip("0").rstrip(".") if "." in value else value)
    return out

def extract_claims(answer: str) -> list[Claim]:
    """Split an answer into sentence-level claims with the evidence numbers each one cites.

    A citation that sits alone after the final punctuation ("... 90 days. [EVIDENCE 1]")
    is attached to the preceding sentence.
    """
    claims: list[Claim] = []
    for line in (answer or "").splitlines():
        line = _LIST_MARKER_RE.sub("", line).strip().lstrip("#").strip().replace("**", "")
        if not line:
            continue
        for fragment in _SENTENCE_SPLIT_RE.split(line):
            fragment = fragment.strip()
            if not fragment:
                continue
            refs = extract_citation_ids(fragment)
            if not strip_citations(fragment).strip() and claims:
                claims[-1].evidence_ids.extend(refs)
                continue
            claims.append(Claim(fragment, refs))
    return claims

def _needs_citation(text: str) -> bool:
    bare = strip_citations(text).strip()
    if bare.endswith(":"):
        return False  # lead-in to a list; the list items carry the citations
    if len(tokenize(bare)) < 4:
        return False  # headings, "Answer:", fragments
    return not _META_RE.search(bare)

def _claim_problem(claim: Claim, evidence: list[dict]) -> str | None:
    bare = strip_citations(claim.text)
    # A citation to evidence that does not exist is wrong however short the sentence is.
    if any(i < 1 or i > len(evidence) for i in claim.evidence_ids):
        return "cites evidence that does not exist"
    if not _needs_citation(claim.text):
        return None
    if not claim.evidence_ids:
        return "uncited"

    cited_text = " ".join((evidence[i - 1].get("text") or "") for i in claim.evidence_ids)
    if not _numbers(bare) <= _numbers(cited_text):
        return "contains a number that is not in the cited evidence"

    # Word overlap is only meaningful when claim and evidence share a script. An English
    # answer drawn from Hindi evidence is a valid translation, so skip the check there
    # (numbers above are still verified).
    claim_terms = [t for t in content_terms(bare) if t.isascii() and not t.isdigit()]
    evidence_terms = {t for t in content_terms(cited_text) if t.isascii() and not t.isdigit()}
    if len(claim_terms) >= 3 and len(evidence_terms) >= _MIN_LATIN_EVIDENCE_TERMS:
        support = sum(t in evidence_terms for t in claim_terms) / len(claim_terms)
        if support < _MIN_TERM_SUPPORT:
            return "is not supported by the cited evidence"
    return None

def verify_answer(answer: str, evidence: list[dict]) -> VerificationResult:
    """Check each claim against only the evidence it cites.

    ``evidence`` must be the list whose numbering the model saw (``[EVIDENCE N]`` is
    1-based into it). Cross-document value differences are reported in ``conflicts``
    but do not by themselves fail verification.
    """
    claims = extract_claims(answer)
    unsupported = [c.text for c in claims if _claim_problem(c, evidence)]
    cited = sorted({i for c in claims for i in c.evidence_ids if 1 <= i <= len(evidence)})
    conflicts = [c.reason for c in detect_numeric_conflicts(evidence, cited)]
    return VerificationResult(
        supported=bool(claims) and not unsupported,
        claims=claims,
        unsupported_claims=unsupported,
        conflicts=conflicts,
    )
