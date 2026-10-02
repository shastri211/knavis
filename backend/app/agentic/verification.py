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
# LaTeX the model adds around formulas (\( 1 \leq n \leq 10^5 \)): markup, not content words.
_LATEX_RE = re.compile(r"\\[A-Za-z]+|\\[()\[\]{}]|\$")

# Minimum share of a claim's (Latin-script) content terms that must appear in the evidence
# it cites. Low on purpose: this catches unrelated or invented statements, not paraphrases.
_MIN_TERM_SUPPORT = 0.3
# Share of a sentence's content terms that evidence must contain to be credited as its source
# (one chunk for an uncited sentence; the whole evidence set for the second-chance check).
_MIN_ATTRIBUTION = 0.6
# Cited evidence with fewer Latin-script content terms than this is treated as non-English.
_MIN_LATIN_EVIDENCE_TERMS = 3
# If at most this share of the substantive sentences fail, the answer is kept without them.
MAX_TRIMMED_SHARE = 0.34

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
    is attached to the preceding sentence. ``Claim.raw`` is the exact text in the answer, so an
    unsupported sentence can be cut out without disturbing the rest.
    """
    claims: list[Claim] = []
    for line in (answer or "").splitlines():
        body = _LIST_MARKER_RE.sub("", line)
        if not body.strip():
            continue
        line_start = len(claims)
        for fragment in _SENTENCE_SPLIT_RE.split(body):
            if not fragment.strip():
                continue
            text = fragment.strip().lstrip("#").strip().replace("**", "")
            refs = extract_citation_ids(fragment)
            if not strip_citations(text).strip() and claims:
                claims[-1].evidence_ids.extend(refs)
                continue
            claims.append(Claim(text, refs, raw=fragment))
        # Models cite a paragraph once, at its end: that citation covers the other sentences of the same line.
        line_ids = sorted({i for c in claims[line_start:] for i in c.evidence_ids})
        for claim in claims[line_start:]:
            if not claim.evidence_ids and line_ids:
                claim.evidence_ids = list(line_ids)
    return claims

def _needs_citation(text: str) -> bool:
    bare = strip_citations(text).strip()
    if bare.endswith(":"):
        return False  # lead-in to a list; the list items carry the citations
    if len(tokenize(bare)) < 4:
        return False  # headings, "Answer:", fragments
    return not _META_RE.search(bare)

def _overlap(terms: set[str], item: dict) -> float:
    return len(terms & set(content_terms(item.get("text") or ""))) / len(terms) if terms else 0.0

def _attribute(text: str, evidence: list[dict]) -> list[int]:
    """Evidence that contains most of an uncited sentence's content terms (local, no model call)."""
    terms = set(content_terms(_LATEX_RE.sub(" ", text)))
    if len(terms) < 3:
        return []
    scored = [(_overlap(terms, item), i) for i, item in enumerate(evidence, 1)]
    best = max((s for s, _ in scored), default=0.0)
    if best < _MIN_ATTRIBUTION:
        return []
    return [i for s, i in scored if s >= best - 0.05][:3]

def _best_sources(text: str, evidence: list[dict]) -> list[int]:
    """The (up to two) chunks sharing the most content terms with a sentence, for repairing its citation."""
    terms = set(content_terms(_LATEX_RE.sub(" ", text)))
    ranked = sorted(((_overlap(terms, item), i) for i, item in enumerate(evidence, 1)), reverse=True)
    return [i for score, i in ranked[:2] if score > 0]

def _problem_with(bare: str, text: str, min_support: float) -> str | None:
    """Why ``bare`` is not supported by ``text`` (numbers first, then shared words), or None."""
    if not _numbers(bare) <= _numbers(text):
        return "contains a number that is not in the cited evidence"
    # Word overlap is only meaningful when claim and evidence share a script. An English
    # answer drawn from Hindi evidence is a valid translation, so skip the check there
    # (numbers above are still verified).
    cleaned = _LATEX_RE.sub(" ", bare)
    claim_terms = [t for t in content_terms(cleaned) if t.isascii() and not t.isdigit()]
    evidence_terms = {t for t in content_terms(text) if t.isascii() and not t.isdigit()}
    if len(claim_terms) >= 3 and len(evidence_terms) >= _MIN_LATIN_EVIDENCE_TERMS:
        if sum(t in evidence_terms for t in claim_terms) / len(claim_terms) < min_support:
            return "is not supported by the cited evidence"
    return None

def _claim_problem(claim: Claim, evidence: list[dict]) -> str | None:
    bare = strip_citations(claim.text)
    # A citation to evidence that does not exist is wrong however short the sentence is.
    if any(i < 1 or i > len(evidence) for i in claim.evidence_ids):
        return "cites evidence that does not exist"
    if not _needs_citation(claim.text):
        return None
    if not claim.evidence_ids:
        # The model gave no citation for this sentence. Accept it only if some evidence plainly contains it; the
        # number and word checks below still apply to that evidence.
        attributed = _attribute(bare, evidence)
        if not attributed:
            return "uncited"
        claim.evidence_ids, claim.attributed = attributed, True

    cited_text = " ".join((evidence[i - 1].get("text") or "") for i in claim.evidence_ids)
    problem = _problem_with(bare, cited_text, _MIN_TERM_SUPPORT)
    if problem is None:
        return None

    # Second chance: models often number the wrong chunk, or label a figure with words from a neighbouring
    # chunk. If the evidence set as a whole supports the sentence (every number present, most words present),
    # accept it and repair its citation to the chunks that really contain it. Invented content still fails.
    everything = " ".join((item.get("text") or "") for item in evidence)
    if _problem_with(bare, everything, _MIN_ATTRIBUTION) is None:
        sources = _best_sources(bare, evidence)
        if sources:
            claim.evidence_ids, claim.attributed = sources, True
            return None
    return problem

def verify_answer(answer: str, evidence: list[dict]) -> VerificationResult:
    """Check each claim against the evidence it cites (or, failing that, the evidence as a whole).

    ``evidence`` must be the list whose numbering the model saw (``[EVIDENCE N]`` is
    1-based into it). Cross-document value differences are reported in ``conflicts``
    but do not by themselves fail verification.
    """
    claims = extract_claims(answer)
    rejected = [c for c in claims if _claim_problem(c, evidence)]
    cited = sorted({i for c in claims for i in c.evidence_ids if 1 <= i <= len(evidence)})
    conflicts = [c.reason for c in detect_numeric_conflicts(evidence, cited)]
    return VerificationResult(
        supported=bool(claims) and not rejected,
        claims=claims,
        unsupported_claims=[c.text for c in rejected],
        conflicts=conflicts,
        rejected=rejected,
        checked=sum(_needs_citation(c.text) for c in claims),
    )

def trim_unsupported(answer: str, result: VerificationResult) -> str | None:
    """The answer without its unsupported sentences, when only a small minority failed; otherwise None.

    Unsupported text never reaches the user. Dropping one embellished sentence from an otherwise correct
    summary is better than discarding the whole answer, and the caller says that it was done.
    """
    if not result.rejected or not result.checked:
        return None
    if len(result.rejected) / result.checked > MAX_TRIMMED_SHARE or len(result.rejected) >= result.checked:
        return None
    trimmed = answer
    for claim in result.rejected:
        trimmed = trimmed.replace(claim.raw, "", 1)
    trimmed = re.sub(r"^[ \t]*[-*•]+[ \t]*$", "", trimmed, flags=re.MULTILINE)        # emptied bullets
    trimmed = re.sub(r"^[ \t]*(?:\[\s*EVIDENCE[^\]]*\][ \t]*)+$", "", trimmed, flags=re.MULTILINE | re.IGNORECASE)  # orphan markers
    trimmed = re.sub(r"[ \t]{2,}", " ", trimmed)
    return re.sub(r"\n{3,}", "\n\n", trimmed).strip()
