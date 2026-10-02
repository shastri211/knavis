def rewrite_query(query: str, failure_reason: str = "") -> str:
    """
    Conservative retrieval rewrite.

    It does not invent facts. It only adds retrieval-oriented cues that are
    already implied by the failed query.
    """
    q = query.strip()
    if not q:
        return q
    suffix = " relevant policy requirements exact terminology"
    if failure_reason:
        suffix += " " + failure_reason[:160]
    return f"{q}{suffix}"
