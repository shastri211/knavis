"""The evaluation harness, its corpus and its dataset. The offline evaluation itself runs here as a regression gate:
if a change makes retrieval, the evidence gate, spreadsheet SQL or routing worse, this fails."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FACT

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "eval" / "dataset.jsonl"
BASELINE = ROOT / "eval" / "baseline_offline.json"


@pytest.fixture(scope="module")
def cases():
    from app.evaluation.runner import load_cases
    return load_cases(DATASET)


@pytest.fixture(scope="module")
def corpus_session(client):
    from app.evaluation.runner import setup_corpus
    session_id, statuses = setup_corpus(client)
    return session_id, statuses


# ---- dataset and corpus ----------------------------------------------------------------------

def test_the_dataset_is_well_formed_and_covers_what_the_readme_promises(cases):
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)) and len(cases) >= 50
    categories = {c.category for c in cases}
    assert {"lookup", "table", "multilingual", "cross_lingual", "summary", "aggregation", "abstain", "routing", "injection"} <= categories
    assert {c.lang for c in cases} >= {"en", "hi", "hinglish"}
    from app.evaluation.corpus import build_corpus
    files = set(build_corpus())
    for c in cases:
        assert c.expect in {"answer", "abstain", "greeting", "utility", "blocked", "rag"}, c.id
        assert set(c.sources) <= files, c.id
        if c.expect == "answer":
            assert c.must_contain and c.sources, c.id
            if c.category == "aggregation":
                assert c.gold_sql and c.gold_result, c.id
            else:
                assert c.evidence_contains, c.id


def test_expected_aggregates_match_an_independent_recomputation_of_the_data(cases):
    """The numbers in the dataset must come from the data, not from the system under test."""
    from app.evaluation.corpus import CAMPAIGN_ROWS, SALES_ROWS
    revenue = {}
    for _, _, region, _, _, amount, _ in SALES_ROWS:
        revenue[region] = revenue.get(region, 0) + amount
    conv = {}
    for _, channel, _, _, converted in CAMPAIGN_ROWS:
        conv[channel] = conv.get(channel, 0) + converted
    assert sum(r[5] for r in SALES_ROWS) == 32970 and max(revenue.items(), key=lambda kv: kv[1]) == ("South", 8545)
    assert sum(r[6] for r in SALES_ROWS) == 7 and sum(r[4] for r in SALES_ROWS) / len(SALES_ROWS) == 16
    assert sum(1 for r in SALES_ROWS if r[2] == "West") == 15
    top = max(conv.values())
    assert sorted(ch for ch, n in conv.items() if n == top) == ["Search", "Social"] and top == 5          # a genuine tie
    assert round(sum(r[3] for r in CAMPAIGN_ROWS) / len(CAMPAIGN_ROWS), 2) == 99.5
    by_id = {c.id: c for c in cases}
    assert by_id["agg_sales_total"].gold_result == [[32970]] and by_id["agg_camp_clicks"].gold_result == [[99.5]]


def test_every_corpus_file_is_ingested(corpus_session):
    _, statuses = corpus_session
    assert statuses and set(statuses.values()) == {"indexed"}, statuses


# ---- the offline evaluation as a regression gate ---------------------------------------------

@pytest.fixture(scope="module")
def offline(client, corpus_session, cases):
    from app.evaluation.runner import run_offline, summarize
    session_id, _ = corpus_session
    outcomes = run_offline(client, session_id, cases, dense_available=False)
    return outcomes, summarize(outcomes, "offline")


def test_offline_floors(offline):
    _, summary = offline
    h = summary["headline"]
    assert h["retrieval_hit_rate"] >= 0.95, h
    assert h["sql_correct_rate"] == 1.0 and h["sql_routed_rate"] == 1.0, h
    assert h["routing_rate"] == 1.0, h
    assert h["abstain_gate_rate"] >= 0.75, h
    assert h["mrr"] >= 0.9, h
    assert summary["skipped"] == 2                       # the two cross-lingual cases need embeddings


def test_offline_is_no_worse_than_the_saved_baseline(offline):
    from app.evaluation.runner import compare_to_baseline
    _, summary = offline
    assert compare_to_baseline(summary, json.loads(BASELINE.read_text(encoding="utf-8"))) == []


def test_the_tied_conversion_question_is_routed_to_sql_and_its_gold_query_shows_both_channels(offline):
    outcomes, _ = offline
    outcome = next(o for o in outcomes if o.id == "agg_camp_conv")
    assert outcome.status == "pass" and outcome.checks == {"routed_to_sql": True, "gold_sql_correct": True}


def test_known_leaks_of_the_lexical_gate_are_reported_not_hidden(offline):
    """Plausible-sounding questions the documents cannot answer still reach the model. The harness must say so."""
    outcomes, _ = offline
    leaked = sorted(o.id for o in outcomes if o.category == "abstain" and o.status == "fail")
    assert leaked == ["abs_headcount", "abs_refund"]


# ---- the live runner's mechanics (fake model) ------------------------------------------------

def test_the_live_runner_counts_calls_judges_answers_and_catches_a_wrong_one(client, corpus_session, cases, llm):
    from app.evaluation.runner import run_live, summarize
    session_id, _ = corpus_session
    picked = [c for c in cases if c.id in {"pol_retention", "abs_football", "route_hello", "inj_ignore", "agg_camp_conv", "pol_backups"}]
    llm.sql = ('SELECT "Channel", SUM("Converted") AS conversions FROM "campaign_results" GROUP BY "Channel" '
               'ORDER BY conversions DESC, "Channel" LIMIT 5')
    llm.answer = f"{FACT} [EVIDENCE 1]"                   # right for the retention question, wrong for the backups question
    outcomes = {o.id: o for o in run_live(client, session_id, picked, "groq", "openai/gpt-oss-20b")}
    assert outcomes["pol_retention"].status == "pass" and outcomes["pol_retention"].calls == 1
    assert outcomes["agg_camp_conv"].status == "pass" and outcomes["agg_camp_conv"].calls == 1
    assert outcomes["abs_football"].status == "pass" and outcomes["abs_football"].calls == 0
    assert outcomes["route_hello"].status == "pass" and outcomes["route_hello"].calls == 0
    assert outcomes["inj_ignore"].status == "pass" and outcomes["inj_ignore"].calls == 0
    wrong = outcomes["pol_backups"]
    assert wrong.status == "fail" and not wrong.checks["correct"] and "30 days" not in llm.answer
    summary = summarize(list(outcomes.values()), "live")
    assert summary["headline"]["total_model_calls"] == 3 and summary["headline"]["answer_accuracy"] == pytest.approx(2 / 3, abs=0.001)


def test_a_tie_answered_with_only_one_winner_is_judged_wrong(client, corpus_session, cases, llm):
    from app.evaluation.runner import run_live
    session_id, _ = corpus_session
    case = next(c for c in cases if c.id == "agg_camp_conv")
    llm.sql = ('SELECT "Channel", SUM("Converted") AS conversions FROM "campaign_results" GROUP BY "Channel" '
               'ORDER BY conversions DESC, "Channel" LIMIT 1')
    [outcome] = run_live(client, session_id, [case], "groq", "openai/gpt-oss-20b")
    assert outcome.status == "fail" and not outcome.checks["correct"]


def test_summary_and_baseline_comparison_unit():
    from app.evaluation.runner import Outcome, compare_to_baseline, contains_all, summarize
    outcomes = [Outcome("a", "lookup", "pass"), Outcome("b", "lookup", "fail"), Outcome("c", "lookup", "skipped")]
    summary = summarize(outcomes, "offline")
    assert summary["by_category"]["lookup"]["rate"] == 0.5 and summary["skipped"] == 1
    assert compare_to_baseline({"headline": {"x": 0.90}}, {"headline": {"x": 0.95}}) == ["x: 0.95 -> 0.9"]
    assert compare_to_baseline({"headline": {"x": 0.94}}, {"headline": {"x": 0.95}}) == []          # within tolerance
    assert compare_to_baseline({"headline": {"latency_p50_s": 9}}, {"headline": {"latency_p50_s": 1}}) == []   # timing is not a quality metric
    assert contains_all("Support: 9 am to 6 pm IST", ["9am", "6pm"]) and contains_all("from 9am", ["9 am"])   # spacing is not accuracy
    assert contains_all("Total: 32,970", ["32970"]) and contains_all("PRO plan is $29", ["$29", "pro"]) and not contains_all("x", ["y"])


def test_live_mode_refuses_to_spend_without_confirmation():
    result = subprocess.run([sys.executable, "-m", "app.evaluation.run", "--mode", "live"], capture_output=True, text=True,
                            cwd=str(Path(__file__).resolve().parents[1]), timeout=120)
    assert result.returncode == 2 and "--yes" in result.stdout
