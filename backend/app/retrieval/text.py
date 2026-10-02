"""Shared, dependency-free text helpers for lexical retrieval and evidence checks.

Everything here is deterministic and local: no model calls.
"""
import re

# Indic scripts keep their vowel signs (matras) inside a word, but Python's \w
# does not match combining marks, which would split Hindi words apart. The danda
# (U+0964, U+0965) is sentence punctuation and is deliberately excluded.
_INDIC = (
    "ऀ-ॣ०-ॿঀ-৿਀-੿઀-૿"
    "଀-୿஀-௿ఀ-౿ಀ-೿ഀ-ൿ"
)
_TOKEN_RE = re.compile(rf"[\w{_INDIC}]+", re.UNICODE)

STOPWORDS = frozenset("""
a an the of in on at to for from by with and or as that this these those it its
i you we they he she me my your our can could should would will shall may might
what who whom whose which when where why how does did do is are was were be been
being has have had tell please give show explain describe about any there if then
than so not no also into over under between during per via
है हैं था थी थे क्या कौन कब कहाँ कैसे क्यों का की के को में से पर और या यह वह ये वो कि तो भी ही नहीं
hai hain tha thi kya kaun kab kahan kaise kyun ka ki ke ko mein se par aur ya yeh woh ye vo bhi nahi
mujhe batao bataiye
kitne kitna kitni tak rakhe rakha rakhi jaate jaata jata jati milta milti milte hota hoti hote karna karne kare karein
liye lie wala wali wale kuch koi kis kisi kaisa kaisi kyon hum aap tum apna apni mera meri hamara hamari sab saare
""".split())

_SUFFIXES = ("ing", "ed", "es", "s")

_OVERVIEW_RES = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\b(summari[sz]e|summary|overview|gist|tl;?dr)\b",
    r"^\s*(what|whats|what's)(\s+is|\s+are)?\s+(this|the|that)\s+"
    r"(document|file|pdf|doc|report|paper|sheet|spreadsheet|presentation|deck|image|audio|recording)s?"
    r"(\s+about|\s+contain\w*)?\s*[?.!]*\s*$",
    r"\bwhat\s+(does|do)\s+(this|the|that|it)\s+\w*\s*(document|file|pdf|doc|report|sheet|presentation|image|audio)?\s*"
    r"(contain|say|cover|include|have)\b",
    r"\b(what|tell me|explain)\b.{0,20}\b(document|file|pdf)\b.{0,12}\babout\b",
    r"सारांश|saransh",
))


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def stem(token: str) -> str:
    """Light inflection stripping for Latin tokens (logs -> log, retained -> retain)."""
    if len(token) < 4 or not token.isascii() or token.isdigit():
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def content_terms(text: str) -> list[str]:
    """Stemmed non-stopword tokens (numbers are kept as-is)."""
    return [
        stem(t) for t in tokenize(text)
        if t not in STOPWORDS and (len(t) > 1 or t.isdigit())
    ]


def lexical_coverage(query: str, text: str) -> tuple[float, int, int]:
    """Fraction of the query's content terms found in ``text``.

    Returns ``(coverage, matched_terms, query_terms)``. Unlike a raw BM25 score this is
    bounded in [0, 1] and independent of corpus size, so it can be thresholded.
    """
    terms = set(content_terms(query))
    if not terms:
        return 0.0, 0, 0
    present = set(content_terms(text))
    matched = len(terms & present)
    return matched / len(terms), matched, len(terms)


def is_overview_query(query: str) -> bool:
    """True for whole-document requests ("summarize this", "what does the file contain")."""
    q = (query or "").strip()
    return bool(q) and any(r.search(q) for r in _OVERVIEW_RES)
