import json
from pathlib import Path
from .metrics import EvalMetrics

class EvaluationHarness:
    """
    Offline harness for routing/safety contract tests.

    It intentionally does not invent ground-truth answers. Exact answer
    correctness requires a labeled corpus and evidence annotations.
    """
    def __init__(self, dataset_path: str):
        self.dataset_path = Path(dataset_path)

    def load(self):
        return [json.loads(x) for x in self.dataset_path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def score_contract(self, records):
        m = EvalMetrics()
        for r in records:
            m.total += 1
            # This harness is designed to be filled by actual application outputs.
            if r.get("route_correct"):
                m.correct_route += 1
            if r.get("grounded_ok"):
                m.grounded_when_required += 1
            if r.get("safe_abstention"):
                m.safe_abstentions += 1
            if r.get("citation_valid"):
                m.citation_valid += 1
        return m.as_dict()
