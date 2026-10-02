"""Deterministic detection of analytical questions about spreadsheet tables. No model is involved.

A question is analytical when it asks for a computation (a total, an average, a count, the highest or lowest
group, a breakdown by a column, a numeric filter) AND it concerns the tables: it names one of their columns,
a value found in a column, the table itself, or the session holds nothing but tables. Retrieval cannot
compute such answers; the SQL path can.
"""
import re
from dataclasses import dataclass, field

from ..retrieval.text import content_terms, tokenize

# Whole-word cues. English, Hindi (Devanagari) and Hinglish; Indic words are matched as whole tokens, so a
# vowel sign inside a word never splits it.
_STRONG_WORDS = frozenset("""
total sum average avg mean median count maximum minimum max min highest lowest most least top bottom largest smallest
biggest greatest best worst fewest rank ranking percentage percent ratio distribution breakdown
कुल औसत कितने कितनी कितना अधिकतम न्यूनतम सर्वाधिक गिनती प्रतिशत
kul ausat kitne kitni kitna adhiktam nyuntam gintee pratishat
""".split())
_STRONG_PHRASES = ("how many", "number of", "no. of", "how much", "count of", "sum of", "average of", "group by",
                   "सबसे ज्यादा", "सबसे अधिक", "सबसे कम", "sabse zyada", "sabse jyada", "sabse kam")
_HIGHEST = ("highest", "most", "top", "maximum", "max", "largest", "biggest", "greatest", "best", "सबसे ज्यादा",
            "सबसे अधिक", "अधिकतम", "सर्वाधिक", "sabse zyada", "sabse jyada", "adhiktam")
_LOWEST = ("lowest", "least", "minimum", "min", "smallest", "worst", "bottom", "fewest", "सबसे कम", "न्यूनतम",
           "sabse kam", "nyuntam")
_GROUP_WORDS = frozenset({"by", "per", "each", "wise", "across"})
_FILTER_PHRASES = ("more than", "less than", "greater than", "fewer than", "at least", "at most", "above", "below",
                   "over", "under", "between", "where", "list all", "show all", "list the", "show the")
_TABLE_WORDS = frozenset({"row", "rows", "record", "records", "entry", "entries", "sheet", "sheets", "spreadsheet",
                          "table", "column", "columns", "csv", "excel", "workbook"})
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass
class Intent:
    strong: bool                                   # the wording asks for a computation
    ranking: str | None = None                     # "highest" | "lowest" | None
    columns: list[tuple[str, str]] = field(default_factory=list)    # (table, column) the question names
    tables: set[str] = field(default_factory=set)  # tables the question names or points at by a column/value


def _words(text: str) -> list[str]:
    """Tokens of a header or file name, splitting underscores and camelCase, stemmed."""
    return content_terms(" ".join(_CAMEL_RE.sub(" ", t) for t in re.split(r"[_\s\-./]+", text or "")))


def same_term(a: str, b: str) -> bool:
    """Equal, or one is a prefix of the other, or they share a long stem ("conversions" ~ "converted")."""
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= 5 and common >= 0.7 * shorter


def _overlap(question_terms: set[str], terms: list[str]) -> bool:
    return bool(terms) and any(same_term(q, t) for q in question_terms for t in terms)


def _has_phrase(padded: str, phrases) -> bool:
    return any(f" {p} " in padded for p in phrases)


def analyze(question: str, tables, has_other_documents: bool) -> Intent | None:
    """``None`` when the question should go to ordinary retrieval; otherwise what it asks for."""
    tokens = tokenize(question)
    if not tokens or not tables:
        return None
    padded = " " + " ".join(tokens) + " "
    token_set = set(tokens)
    strong = bool(token_set & _STRONG_WORDS) or _has_phrase(padded, _STRONG_PHRASES)
    group_by = bool(token_set & _GROUP_WORDS)
    has_number = any(t.isdigit() for t in tokens)
    filter_cue = _has_phrase(padded, _FILTER_PHRASES) and (has_number or " where " in padded or "list" in token_set or "show" in token_set)

    q_terms = set(content_terms(question))
    intent = Intent(strong=strong)
    for table in tables:
        if _overlap(q_terms, _words(table.table_name) + _words(table.sheet or "") + _words(table.filename.rsplit(".", 1)[0])):
            intent.tables.add(table.table_name)
        for column in table.columns_json:
            named = _overlap(q_terms, _words(column["original"]) + _words(column["name"]))
            valued = column["kind"] == "text" and any(
                _overlap(q_terms, content_terms(value)) for value in column.get("values", []))
            if named or valued:
                intent.columns.append((table.table_name, column["name"]))
                intent.tables.add(table.table_name)

    mentions = bool(intent.columns) or bool(intent.tables) or bool(token_set & _TABLE_WORDS)
    if strong and (mentions or not has_other_documents):
        pass   # asks for a computation about the tables (or the tables are all there is)
    elif intent.columns and (group_by or filter_cue):
        pass   # "sales by region", "rows where channel is Email"
    else:
        return None

    ranked = padded.replace(" at least ", " ").replace(" at most ", " ")   # filters, not "least"/"most"
    lowest, highest = _has_phrase(ranked, _LOWEST), _has_phrase(ranked, _HIGHEST)
    if lowest != highest:
        intent.ranking = "lowest" if lowest else "highest"
    return intent
