from dataclasses import dataclass, field
from typing import Any, Literal

AgentName = Literal["greeting", "guardrails", "rag"]

@dataclass
class AgentRequest:
    session_id: str
    text: str
    language: str | None = None
    provider: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class AgentDecision:
    allowed: bool
    route: Literal["greeting", "rag", "blocked", "utility"]
    reason: str
    language: str | None = None
    intent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class AgentResponse:
    text: str
    route: str
    grounded: bool = False
    citations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
