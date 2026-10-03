"""Command line for the evaluation.  Run from ``backend``:

    python -m app.evaluation.run                      # offline: free, no keys, deterministic
    python -m app.evaluation.run --mode live --yes    # live: asks every question through a real provider (spends model calls)
    python -m app.evaluation.run --check ../eval/baseline_offline.json     # exit 1 if a metric got worse

The run uses its own temporary data directory and account, so it never touches your chats or documents.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
KEYS = ("NVIDIA_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "ASSEMBLYAI_API_KEY", "MISTRAL_API_KEY", "GEMINI_API_KEY")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--dataset", default=str(ROOT / "eval" / "dataset.jsonl"))
    parser.add_argument("--only", help="run only this category (e.g. aggregation)")
    parser.add_argument("--provider", default=None, help="live mode: provider (default: the configured default)")
    parser.add_argument("--model", default=None, help="live mode: model (default: the configured default)")
    parser.add_argument("--yes", action="store_true", help="live mode: confirm that model calls may be spent")
    parser.add_argument("--report", default=None, help="write the full JSON report here (default: eval/results/<mode>.json)")
    parser.add_argument("--check", default=None, help="compare with a saved baseline and fail if a metric dropped")
    parser.add_argument("--save-baseline", default=None, help="write the headline metrics to this file")
    args = parser.parse_args(argv)

    # Settings are read when the app is imported, so decide the environment first.
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="knavis_eval_")
    os.environ["QDRANT_URL"] = ""
    os.environ["STORAGE_BACKEND"] = "local"   # the synthetic corpus never goes to a real bucket or database, whatever .env holds
    os.environ["DATABASE_URL"] = ""
    os.environ["AUTH_ENABLED"] = "true"
    for name in ("RATE_LIMIT_CHAT_PER_MINUTE", "RATE_LIMIT_UPLOAD_PER_MINUTE", "RATE_LIMIT_AUTH_PER_MINUTE"):
        os.environ[name] = "0"
    if args.mode == "offline":
        for key in KEYS:
            os.environ[key] = ""        # nothing hosted can be called, whatever .env holds

    from fastapi.testclient import TestClient
    from ..main import app
    from . import runner

    cases = runner.load_cases(args.dataset)
    if args.only:
        cases = [c for c in cases if c.category == args.only]

    if args.mode == "live":
        from ..config import settings
        from ..providers import resolve_selection
        provider, model = resolve_selection(args.provider, args.model)
        planned = sum(1 for c in cases if c.expect in ("answer", "abstain") and c.category != "routing")
        print(f"Live evaluation with {provider}/{model}: up to {planned} model calls "
              f"(one per question that reaches the model){'; embeddings are used too' if settings.nvidia_api_key else ''}.")
        if not args.yes:
            print("Re-run with --yes to spend them.")
            return 2

    with TestClient(app) as client:
        token = client.post("/api/auth/register", json={"email": "evaluation@example.com", "password": "evaluation-run-passphrase"}).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        session_id, statuses = runner.setup_corpus(client)
        bad = {name: status for name, status in statuses.items() if status != "indexed"}
        if bad:
            print(f"Warning: some corpus files were not indexed: {bad}")
        if args.mode == "offline":
            from ..integration.pipeline import get_pipeline
            outcomes = runner.run_offline(client, session_id, cases, dense_available=get_pipeline().qdrant is not None)
        else:
            outcomes = runner.run_live(client, session_id, cases, provider, model)

    summary = runner.summarize(outcomes, args.mode)
    print(runner.format_report(summary, outcomes))

    report = Path(args.report) if args.report else ROOT / "eval" / "results" / f"{args.mode}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({**summary, "outcomes": [o.__dict__ for o in outcomes]}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nFull report: {report}")
    if args.save_baseline:
        Path(args.save_baseline).write_text(json.dumps({"mode": args.mode, "headline": summary["headline"]}, indent=1) + "\n", encoding="utf-8")
        print(f"Baseline saved: {args.save_baseline}")
    if args.check:
        worse = runner.compare_to_baseline(summary, json.loads(Path(args.check).read_text(encoding="utf-8")))
        if worse:
            print("\nWorse than the baseline:\n  " + "\n  ".join(worse))
            return 1
        print("\nNo metric is worse than the baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
