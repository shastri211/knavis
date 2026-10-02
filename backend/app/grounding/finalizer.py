from .citations import validate_citations, source_citations

ABSTENTION = (
    "I don't have enough reliable evidence in the provided material to answer "
    "that accurately, so I won't guess."
)

def finalize_answer(answer: str, evidence: list[dict], require_citations=True):
    if not answer or not answer.strip():
        return ABSTENTION, [], False

    ok, refs = validate_citations(answer, evidence)
    if require_citations and not ok:
        return ABSTENTION, [], False

    citations = source_citations(
        [evidence[i - 1] for i in refs] if refs else evidence
    )
    return answer.strip(), citations, True
