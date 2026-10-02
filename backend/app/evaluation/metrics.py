from dataclasses import dataclass

@dataclass
class EvalMetrics:
    total: int = 0
    correct_route: int = 0
    grounded_when_required: int = 0
    safe_abstentions: int = 0
    citation_valid: int = 0

    @property
    def routing_accuracy(self):
        return self.correct_route / self.total if self.total else 0.0

    @property
    def grounding_rate(self):
        return self.grounded_when_required / self.total if self.total else 0.0

    @property
    def safe_abstention_rate(self):
        return self.safe_abstentions / self.total if self.total else 0.0

    @property
    def citation_validity(self):
        return self.citation_valid / self.total if self.total else 0.0

    def as_dict(self):
        return {
            "total": self.total,
            "routing_accuracy": round(self.routing_accuracy, 4),
            "grounding_rate": round(self.grounding_rate, 4),
            "safe_abstention_rate": round(self.safe_abstention_rate, 4),
            "citation_validity": round(self.citation_validity, 4),
        }
