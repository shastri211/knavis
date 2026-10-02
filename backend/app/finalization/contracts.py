from dataclasses import dataclass
from typing import Any

@dataclass
class FinalAnswer:
    text: str
    grounded: bool
    citations: list[dict[str, Any]]
    route: str
    provider: str | None
    model: str | None
    verification: str

@dataclass
class PipelineHealth:
    ingestion: str
    retrieval: str
    generation: str
    multimodal: str
    overall: str
