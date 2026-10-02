"""Run the evaluation dataset against the real application, offline (free) or live (spends model calls).

Offline mode needs no model and no keys. It measures the parts that decide whether an answer CAN be right:
* retrieval: does the evidence the gate hands to the model contain the gold fact, from the right file?
* the gate: do questions the documents cannot answer reach the model at all (they must not)?
* analytics: does gold SQL give the expected numbers over the loaded tables, and are such questions routed to SQL?
* routing: are greetings, utilities and prompt injection handled by rules, with no model involved?

Live mode asks every question through ``POST /api/chat`` with a real provider and additionally measures the answers:
correctness, citations, abstention, model calls and latency.
"""
import asyncio
import json
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

ABSTAIN_MARKERS = ("don't have enough reliable evidence", "couldn't compute that reliably", "could not verify", "won't guess")
ANSWER_CATEGORIES = {"lookup", "table", "multilingual", "cross_lingual", "summary"}
ROUTING_CATEGORIES = {"routing", "injection"}


@dataclass
class Case:
    id: str
    category: str
    question: str
    expect: str                        # answer | abstain | greeting | utility | blocked | rag
    lang: str = "en"
    must_contain: list = field(default_factory=list)
    must_not_contain: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    evidence_contains: list = field(default_factory=list)
    gold_sql: str | None = None
    gold_result: list | None = None
    intent: str | None = None
    requires_dense: bool = False
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Case":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Outcome:
    id: str
    category: str
    status: str                        # pass | fail | skipped
    checks: dict = field(default_factory=dict)
    detail: str = ""
    calls: int | None = None
    tokens: int | None = None
    seconds: float | None = None
    rank: int | None = None            # offline: 1-based position of the first candidate holding the gold fact


def load_cases(path: str | Path) -> list[Case]:
    return [Case.from_dict(json.loads(line)) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(",", "")).casefold()


def contains_all(text: str, needles) -> bool:
    haystack = _norm(text)
    return all(_norm(n) in haystack for n in needles)


# ---- corpus --------------------------------------------------------------------------------

def setup_corpus(client) -> tuple[str, dict]:
    """Create a chat in the signed-in client and upload the whole corpus. Returns ``(session id, {file: status})``."""
    from .corpus import build_corpus
    session_id = client.post("/api/sessions", json={"title": "evaluation"}).json()["id"]
    for name, (data, content_type) in build_corpus().items():
        response = client.post("/api/uploads", data={"session_id": session_id}, files={"file": (name, data, content_type)})
        if response.status_code != 200:
            raise RuntimeError(f"Could not upload {name}: {response.text[:200]}")
    # Ingestion is a background task; wait until every document has left the queue.
    deadline = time.monotonic() + 120
    while True:
        documents = client.get(f"/api/sessions/{session_id}/documents").json()
        if all(d["status"] not in ("queued", "uploaded") for d in documents) or time.monotonic() > deadline:
            return session_id, {d["filename"]: d["status"] for d in documents}
        time.sleep(0.5)


# ---- offline -------------------------------------------------------------------------------

def _offline_retrieval(case: Case, session_id: str) -> Outcome:
    from ..config import settings
    from ..grounding.evidence_gate import assess_evidence
    from ..integration.pipeline import get_pipeline
    candidates = asyncio.run(get_pipeline().retrieve(session_id, case.question, settings.top_k_rerank))
    decision = assess_evidence(case.question, candidates)
    if case.expect == "abstain":
        ok = not decision.sufficient
        return Outcome(case.id, case.category, "pass" if ok else "fail", {"gate_rejects": ok},
                       "" if ok else f"the gate would pass {len(decision.selected)} chunk(s) to the model, e.g. from "
                                     f"{(decision.selected[0].get('metadata') or {}).get('source')}")
    pool = [c for c in decision.selected if (c.get("metadata") or {}).get("source") in case.sources] if case.sources else decision.selected
    in_gate = contains_all(" ".join(c.get("text", "") for c in pool), case.evidence_contains)
    source_ok = bool(pool)
    rank = next((i for i, c in enumerate(candidates, 1)
                 if (c.get("metadata") or {}).get("source") in case.sources and contains_all(c.get("text", ""), case.evidence_contains)), None)
    ok = in_gate and source_ok
    detail = "" if ok else ("the right file was not selected" if not source_ok else f"the gold fact {case.evidence_contains} is not in the selected evidence")
    return Outcome(case.id, case.category, "pass" if ok else "fail",
                   {"evidence_has_gold_fact": in_gate, "right_source_selected": source_ok}, detail, rank=rank)


def _same_rows(got, want) -> bool:
    if len(got) != len(want):
        return False
    for g, w in zip(got, want):
        if len(g) != len(w):
            return False
        for a, b in zip(g, w):
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if abs(float(a) - float(b)) > 0.005:
                    return False
            elif a != b:
                return False
    return True


def _offline_analytics(case: Case, session_id: str) -> Outcome:
    from ..analytics.detect import analyze
    from ..analytics.sqlguard import SqlRejected, run_select
    from ..analytics.tablestore import session_tables
    from ..db import SessionLocal
    with SessionLocal() as db:
        tables = session_tables(db, session_id)
        for t in tables:
            db.expunge(t)
    routed = analyze(case.question, tables, has_other_documents=True) is not None
    try:
        result = run_select(session_id, case.gold_sql, tables)
        correct = _same_rows([list(r) for r in result.rows], case.gold_result)
        detail = "" if correct else f"gold SQL returned {result.rows[:4]}, expected {case.gold_result[:4]}"
    except SqlRejected as exc:
        correct, detail = False, f"gold SQL was refused: {exc}"
    ok = routed and correct
    if not routed:
        detail = (detail + " " if detail else "") + "the question is not recognised as analytical, so it would go to retrieval"
    return Outcome(case.id, case.category, "pass" if ok else "fail", {"routed_to_sql": routed, "gold_sql_correct": correct}, detail)


def _offline_routing(case: Case) -> Outcome:
    from ..agents.contracts import AgentRequest
    from ..agents.guardrails_agent import GuardrailsAgent
    from ..agents.semantic_router import SemanticRouter

    class NoModelRouter:
        async def classify(self, *args, **kwargs):
            raise AssertionError("the router asked for a model call")

    if case.expect == "blocked":
        decision = asyncio.run(GuardrailsAgent(NoModelRouter(), has_documents=lambda _s: True).inspect(AgentRequest("s", case.question)))
        ok = not decision.allowed and decision.route == "blocked"
        return Outcome(case.id, case.category, "pass" if ok else "fail", {"blocked_without_a_model": ok})
    rule = SemanticRouter._deterministic(case.question)
    if case.expect == "rag":
        ok = rule is None
    else:
        ok = rule is not None and rule.intent == case.intent
    return Outcome(case.id, case.category, "pass" if ok else "fail", {"routed_by_rules": ok},
                   "" if ok else f"rules decided {getattr(rule, 'intent', None)!r}, expected {case.intent!r}")


def run_offline(client, session_id: str, cases: list[Case], dense_available: bool = False) -> list[Outcome]:
    outcomes = []
    for case in cases:
        if case.requires_dense and not dense_available:
            outcomes.append(Outcome(case.id, case.category, "skipped", detail="needs dense retrieval (embeddings)"))
        elif case.category == "aggregation":
            outcomes.append(_offline_analytics(case, session_id))
        elif case.category in ROUTING_CATEGORIES:
            outcomes.append(_offline_routing(case))
        else:
            outcomes.append(_offline_retrieval(case, session_id))
    return outcomes


# ---- live ----------------------------------------------------------------------------------

class CallCounter:
    """Wraps the provider call to count model calls and tokens per question."""

    def __init__(self):
        self.calls = 0
        self.tokens = 0

    def install(self):
        from .. import provider_service
        original = provider_service.raw_chat

        async def counted(provider, model, messages, **kwargs):
            response = await original(provider, model, messages, **kwargs)
            self.calls += 1
            self.tokens += int((response.usage or {}).get("total_tokens") or 0)
            return response

        provider_service.raw_chat = counted
        self._restore = lambda: setattr(provider_service, "raw_chat", original)

    def uninstall(self):
        self._restore()


def run_live(client, session_id: str, cases: list[Case], provider: str, model: str) -> list[Outcome]:
    counter = CallCounter()
    counter.install()
    outcomes = []
    try:
        for case in cases:
            counter.calls = counter.tokens = 0
            started = time.perf_counter()
            response = client.post("/api/chat", json={"session_id": session_id, "content": case.question, "provider": provider, "model": model})
            seconds = time.perf_counter() - started
            if response.status_code != 200:
                outcomes.append(Outcome(case.id, case.category, "fail", {"http_ok": False}, f"HTTP {response.status_code}: {response.text[:120]}",
                                        counter.calls, counter.tokens, seconds))
                continue
            body = response.json()
            outcomes.append(_judge_live(case, body, counter.calls, counter.tokens, seconds))
    finally:
        counter.uninstall()
    return outcomes


def _judge_live(case: Case, body: dict, calls: int, tokens: int, seconds: float) -> Outcome:
    answer, route, citations = body["message"]["content"], body["route"], body.get("citations") or []
    abstained = any(m in answer.casefold() for m in ABSTAIN_MARKERS)
    sources = {c.get("source") for c in citations}
    checks: dict[str, bool] = {}
    if case.expect == "answer":
        checks["answered"] = not abstained and route == "rag"
        checks["correct"] = contains_all(answer, case.must_contain) and not any(_norm(n) in _norm(answer) for n in case.must_not_contain)
        checks["cited"] = bool(citations)
        checks["right_source"] = bool(sources & set(case.sources)) if case.sources else bool(citations)
        checks["one_call"] = calls <= 1
    elif case.expect == "abstain":
        checks["abstained"] = abstained
        checks["no_citations"] = not citations
        checks["no_hallucinated_answer"] = abstained
    else:
        checks["right_route"] = route == case.expect
        checks["no_model_call"] = calls == 0 if case.expect != "rag" else True
    failed = [name for name, ok in checks.items() if not ok]
    detail = "" if not failed else f"failed: {', '.join(failed)}; answer: {answer[:160]!r}"
    return Outcome(case.id, case.category, "fail" if failed else "pass", checks, detail, calls, tokens, seconds)


# ---- summary -------------------------------------------------------------------------------

def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def summarize(outcomes: list[Outcome], mode: str) -> dict:
    by_category: dict[str, dict] = {}
    for o in outcomes:
        c = by_category.setdefault(o.category, {"n": 0, "pass": 0, "fail": 0, "skipped": 0})
        c["n"] += 1
        c[o.status] += 1
    for c in by_category.values():
        c["rate"] = _rate(c["pass"], c["pass"] + c["fail"])

    def rate(categories=None, ids=None, check=None):
        pool = [o for o in outcomes if o.status != "skipped" and (categories is None or o.category in categories)]
        if check:
            pool = [o for o in pool if check in o.checks]
            return _rate(sum(o.checks[check] for o in pool), len(pool))
        return _rate(sum(o.status == "pass" for o in pool), len(pool))

    headline: dict = {}
    if mode == "offline":
        headline = {
            "retrieval_hit_rate": rate(ANSWER_CATEGORIES),
            "abstain_gate_rate": rate({"abstain"}),
            "sql_correct_rate": rate({"aggregation"}, check="gold_sql_correct"),
            "sql_routed_rate": rate({"aggregation"}, check="routed_to_sql"),
            "routing_rate": rate(ROUTING_CATEGORIES),
        }
        ranks = [o.rank for o in outcomes if o.category in ANSWER_CATEGORIES and o.status != "skipped"]
        headline["mrr"] = round(sum(1 / r for r in ranks if r) / len(ranks), 4) if ranks else None
    else:
        answerable = {"lookup", "table", "multilingual", "cross_lingual", "summary", "aggregation"}
        headline = {
            "answer_accuracy": rate(answerable, check="correct"),
            "answered_rate": rate(answerable, check="answered"),
            "citation_rate": rate(answerable, check="cited"),
            "right_source_rate": rate(answerable, check="right_source"),
            "abstain_accuracy": rate({"abstain"}, check="abstained"),
            "routing_rate": rate(ROUTING_CATEGORIES),
        }
        timed = [o for o in outcomes if o.seconds is not None and o.calls]
        calls = [o.calls for o in outcomes if o.category in answerable and o.calls is not None]
        if timed:
            seconds = sorted(o.seconds for o in timed)
            headline["latency_p50_s"] = round(statistics.median(seconds), 2)
            headline["latency_p95_s"] = round(seconds[min(len(seconds) - 1, int(0.95 * len(seconds)))], 2)
        headline["mean_model_calls_per_answerable_question"] = round(sum(calls) / len(calls), 3) if calls else None
        headline["total_model_calls"] = sum(o.calls or 0 for o in outcomes)
        headline["total_tokens"] = sum(o.tokens or 0 for o in outcomes)
    return {"mode": mode, "cases": len(outcomes), "passed": sum(o.status == "pass" for o in outcomes),
            "failed": sum(o.status == "fail" for o in outcomes), "skipped": sum(o.status == "skipped" for o in outcomes),
            "headline": headline, "by_category": by_category}


def format_report(summary: dict, outcomes: list[Outcome]) -> str:
    lines = [f"Evaluation ({summary['mode']}): {summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped of {summary['cases']}", ""]
    lines.append("Headline metrics")
    for key, value in summary["headline"].items():
        lines.append(f"  {key:<44}{'n/a' if value is None else value}")
    lines += ["", "By category", f"  {'category':<16}{'n':>4}{'pass':>6}{'fail':>6}{'skip':>6}{'rate':>8}"]
    for name, c in sorted(summary["by_category"].items()):
        lines.append(f"  {name:<16}{c['n']:>4}{c['pass']:>6}{c['fail']:>6}{c['skipped']:>6}{'' if c['rate'] is None else format(c['rate'], '.2f'):>8}")
    failures = [o for o in outcomes if o.status == "fail"]
    if failures:
        lines += ["", "Failures"]
        lines += [f"  [{o.category}] {o.id}: {o.detail}" for o in failures]
    return "\n".join(lines)


def compare_to_baseline(summary: dict, baseline: dict, tolerance: float = 0.02) -> list[str]:
    """Metrics that got worse than the saved baseline by more than ``tolerance``."""
    worse = []
    for key, old in baseline.get("headline", {}).items():
        new = summary["headline"].get(key)
        if old is None or new is None or key.startswith(("latency", "total_", "mean_")):
            continue
        if new < old - tolerance:
            worse.append(f"{key}: {old} -> {new}")
    return worse
