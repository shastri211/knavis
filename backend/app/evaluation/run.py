from pathlib import Path
from .harness import EvaluationHarness

if __name__ == "__main__":
    path = Path(__file__).resolve().parents[3] / "eval" / "dataset.jsonl"
    h = EvaluationHarness(str(path))
    data = h.load()
    print(f"Loaded {len(data)} evaluation cases.")
    print("Dataset contract is ready. Connect live application outputs to score metrics.")
    print(h.score_contract([]))
