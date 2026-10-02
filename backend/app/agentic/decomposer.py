import re

def decompose_query(query: str, max_subqueries=3) -> list[str]:
    """
    Lightweight decomposition for explicit multi-part questions.

    This is deliberately conservative. A future LLM planner can replace it,
    but must still obey max_subqueries.
    """
    parts = re.split(r"\s+(?:and|also|plus|as well as)\s+", query, flags=re.I)
    parts = [p.strip(" ?.") for p in parts if p.strip()]
    if len(parts) <= 1:
        return [query]
    return parts[:max_subqueries]
