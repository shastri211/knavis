"""Offline experiment. Run from the repository root:  python eval/experiments/gate_variants.py

Does any lexical evidence-gate variant separate look-alike unanswerable questions ("refund policy for enterprise
customers", "how many employees") from answerable ones? Result, recorded in eval/README.md: none does. Every setting
that blocks the first also blocks legitimate questions, and the second cannot be separated lexically at all.
"""
import asyncio
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gate_")
for key in ("NVIDIA_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "ASSEMBLYAI_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY", "QDRANT_URL"):
    os.environ[key] = ""
for key in ("RATE_LIMIT_CHAT_PER_MINUTE", "RATE_LIMIT_UPLOAD_PER_MINUTE", "RATE_LIMIT_AUTH_PER_MINUTE"):
    os.environ[key] = "0"
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.evaluation import runner  # noqa: E402
from app.integration.pipeline import get_pipeline  # noqa: E402
from app.main import app  # noqa: E402
from app.models import DocChunk  # noqa: E402
from app.retrieval.text import content_terms  # noqa: E402

QUESTION_WORDS = {"long", "many", "much", "often", "per"}
VARIANTS = [
    {"name": "baseline 0.50", "stop": False, "idf": False, "thr": 0.5},
    {"name": "stop(long,many,much..) 0.50", "stop": True, "idf": False, "thr": 0.5},
    {"name": "stop + 0.60", "stop": True, "idf": False, "thr": 0.6},
    {"name": "idf 0.45", "stop": False, "idf": True, "thr": 0.45},
    {"name": "stop + idf 0.45", "stop": True, "idf": True, "thr": 0.45},
    {"name": "stop + idf 0.50", "stop": True, "idf": True, "thr": 0.50},
]


def main():
    cases = [c for c in runner.load_cases(os.path.join(ROOT, "eval", "dataset.jsonl"))
             if (c.category in runner.ANSWER_CATEGORIES or c.category == "abstain") and not c.requires_dense]
    with TestClient(app) as client:
        token = client.post("/api/auth/register", json={"email": "gate@example.com", "password": "gate experiment pass"}).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        session_id, _ = runner.setup_corpus(client)
        with SessionLocal() as db:
            chunk_terms = [set(content_terms(c.text)) for c in db.query(DocChunk).filter(DocChunk.session_id == session_id)]
        n = len(chunk_terms)
        pipeline = get_pipeline()

        def passes(question, text, variant):
            terms = {t for t in content_terms(question) if not (variant["stop"] and t in QUESTION_WORDS)}
            if not terms:
                return False
            present = set(content_terms(text))
            if variant["idf"]:
                weight = {t: math.log(1 + n / (1 + sum(t in s for s in chunk_terms))) for t in terms}
                coverage = sum(weight[t] for t in terms & present) / sum(weight.values())
            else:
                coverage = len(terms & present) / len(terms)
            return coverage >= variant["thr"] and bool(terms & present)

        candidates = {c.id: asyncio.run(pipeline.retrieve(session_id, c.question, 12)) for c in cases}
        print(f"{'variant':<30}{'answerable ok':>15}{'unanswerable ok':>17}   missed answerable / leaked unanswerable")
        for variant in VARIANTS:
            answerable = unanswerable = 0
            ok_answerable = ok_unanswerable = 0
            missed, leaked = [], []
            for case in cases:
                pool = candidates[case.id]
                if case.expect == "answer":
                    answerable += 1
                    hit = any((x.get("metadata") or {}).get("source") in case.sources and passes(case.question, x["text"], variant)
                              and runner.contains_all(x["text"], case.evidence_contains) for x in pool)
                    ok_answerable += hit
                    if not hit:
                        missed.append(case.id)
                else:
                    unanswerable += 1
                    leak = any(passes(case.question, x["text"], variant) for x in pool)
                    ok_unanswerable += not leak
                    if leak:
                        leaked.append(case.id)
            print(f"{variant['name']:<30}{ok_answerable:>9}/{answerable:<5}{ok_unanswerable:>11}/{unanswerable:<5}   {missed} / {leaked}")


if __name__ == "__main__":
    main()
