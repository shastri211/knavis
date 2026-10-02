from dataclasses import dataclass, field
from typing import Literal

Action = Literal["retrieve", "rewrite", "decompose", "verify", "answer", "abstain"]

@dataclass
class AgentStep:
    action: Action
    reason: str
    query: str | None = None
    status: str = "planned"

@dataclass
class AgentPlan:
    original_query: str
    steps: list[AgentStep] = field(default_factory=list)
    max_retrieval_calls: int = 2
    max_subqueries: int = 3

@dataclass
class Claim:
    text: str
    evidence_ids: list[int] = field(default_factory=list)
    attributed: bool = False   # the citation was found locally, not written by the model
    raw: str = ""              # the exact text in the answer (for cutting an unsupported sentence out)

@dataclass
class VerificationResult:
    supported: bool
    claims: list[Claim]
    unsupported_claims: list[str]
    conflicts: list[str]
    rejected: list[Claim] = field(default_factory=list)
    checked: int = 0           # how many sentences needed support (headings and courtesy lines do not)
