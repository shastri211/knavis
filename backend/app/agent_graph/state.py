from typing import Any, TypedDict

class AgentState(TypedDict, total=False):
    session_id: str
    text: str
    language: str | None
    provider: str | None
    model: str | None
    route: str | None
    intent: str | None
    guardrail_allowed: bool
    guardrail_reason: str | None
    answer: str | None
    grounded: bool
    citations: list[dict[str, Any]]
    verification: str | None
    metadata: dict[str, Any]
    error: str | None
