# Evaluation

A fixed set of 56 questions over a small synthetic corpus ("Northwind Retail"): policy text, a Word handbook with a
table, a PDF price list, a Hindi notice, subtitles, a sales workbook and a campaign CSV. Every document is generated
from code (`backend/app/evaluation/corpus.py`) and every expected number is recomputed independently in the tests, so
the ground truth never comes from the system being measured.

```bash
cd backend
python -m app.evaluation.run                       # offline: free, no keys, deterministic (this is what CI runs)
python -m app.evaluation.run --mode live --yes     # live: every question through a real provider; spends model calls
python -m app.evaluation.run --check ../eval/baseline_offline.json   # exit 1 if a metric got worse
python -m app.evaluation.run --only aggregation    # one category
```

The run uses a temporary data directory and its own throwaway account; it never touches your chats.

## What the dataset covers

| Category | Cases | Notes |
|---|---|---|
| lookup / table | 18 | facts in text, a Word table and a PDF |
| multilingual | 5 | Hindi on a Hindi document, Hinglish on English documents |
| cross_lingual | 2 | Hindi question about an English document and the reverse: needs embeddings |
| summary | 1 | whole-document request |
| aggregation | 12 | totals, averages, counts, highest by group, a genuine tie (Search and Social both 5), Hindi |
| abstain | 8 | questions the documents cannot answer, including look-alikes ("refund policy", "how many employees") |
| routing | 7 | greetings, thanks, capabilities, date/time, and "what time does the backup run?" (must stay a document question) |
| injection | 3 | prompt-injection attempts that must be blocked without a model call |

## Offline metrics (no model, no keys)

| Metric | Meaning |
|---|---|
| `retrieval_hit_rate` | the evidence the gate hands to the model contains the gold fact, from the right file |
| `mrr` | how high the first gold chunk ranks among the retrieved candidates |
| `abstain_gate_rate` | questions the documents cannot answer that the gate refuses to pass to the model |
| `sql_routed_rate` / `sql_correct_rate` | spreadsheet questions recognised as analytical; gold SQL returns the expected rows |
| `routing_rate` | greetings, utilities and injection handled by rules with no model call |

Cases that need embeddings are reported as skipped, not passed.

## Live metrics

`answer_accuracy` (every required fact present, nothing forbidden), `answered_rate`, `citation_rate`,
`right_source_rate`, `abstain_accuracy`, `routing_rate`, `mean_model_calls_per_answerable_question` (target: 1),
`latency_p50_s` / `latency_p95_s`, and total model calls and tokens. A tie answered with only one winner counts as wrong.

## Current result and its limits

Offline baseline (`baseline_offline.json`): retrieval 1.00, SQL 1.00, routing 1.00, abstain gate **0.75**: the lexical
gate lets two look-alike questions through (`abs_refund`, `abs_headcount`), where the model must then abstain. That is
a real weakness, kept visible on purpose: a test asserts exactly these two leaks, so fixing the gate will make that
test ask for an update to the baseline, and a regression elsewhere fails CI.

## First live run (2026-10-02, Groq `openai/gpt-oss-20b`, NVIDIA embeddings)

First pass over all 56 questions: 54 correct. Both misses were informative:

- `pr_support`: the model wrote "9 am to 6 pm" with a narrow no-break space and the scorer wanted "9am". The answer was right;
  the scorer now ignores whitespace.
- `agg_camp_clicks` ("average clicks per campaign"): the model grouped by the campaign id and returned one average per row.
  A genuine mistake. The SQL prompt now says that "per X" where each row *is* an X means one figure over all rows.

Re-running the aggregation (12) and lookup (15) categories after those two changes: 27 of 27 correct. Treat that second
result with care: the two fixes were made after seeing these very questions, so it shows the fixes work, not that the
system is 100% accurate on unseen questions.

| Metric (first full pass) | Value |
|---|---|
| answer accuracy | 0.95 (rest of the corpus: 1.00 after the fixes above) |
| every answer cited, from the right file | 1.00 / 1.00 |
| abstained when it should | 1.00 (8 of 8; the two look-alike leaks the offline gate shows were caught by the model) |
| routing and injection | 1.00, with zero model calls |
| model calls per answerable question | 1.0 (41 calls in total, about 22,000 tokens) |
| latency p50 / p95 | 3.4 s / 10.6 s |
| cross-lingual (Hindi question on an English document, and the reverse) | 2 of 2 with embeddings |

One abstention cost two calls: a "how many ..." question looked analytical, the SQL writer declined it, and retrieval
then answered ("not found"). The latency is the hosted model's generation time, not local work.

## Why the evidence gate was not changed (Phase 5 experiment)

The two look-alike leaks (`abs_refund`, `abs_headcount`) looked like a gate-tuning problem. `eval/experiments/gate_variants.py`
tries the obvious lexical fixes on the 24 answerable and 8 unanswerable keyword-reachable cases:

| Variant | answerable kept | unanswerable blocked | what it broke |
|---|---|---|---|
| baseline (coverage >= 0.50) | 24 / 24 | 6 / 8 | |
| also ignore "long, many, much, often, per" | 24 / 24 | 6 / 8 | nothing changed |
| coverage >= 0.60 | 22 / 24 | 7 / 8 | `pr_support`, `hinglish_backup` |
| inverse-document-frequency weighting, >= 0.45 | 22 / 24 | 6 / 8 | `pr_support`, `hinglish_backup` |
| ignore words + idf, >= 0.50 | 21 / 24 | 7 / 8 | also `srt_growth` |

Every setting that blocks "refund policy for enterprise customers" also blocks legitimate questions whose answer uses
different words ("support hours" vs "9am to 6pm"), because both look the same lexically: one matching word, one absent
word. "How many employees does Northwind Retail have?" cannot be separated at all: every one of its words occurs in the
corpus. So the gate stays as it is, the leaks stay visible, and the model's instruction to abstain (which held 8 of 8 in
the live run) is the backstop. A different signal, such as the dense similarity of the best chunk, is the next thing to
try, and it needs the embedding provider to evaluate.

What this does **not** prove: string matching cannot grade wording, the corpus is small and synthetic, scanned pages
and audio are not covered (they need paid OCR/transcription), and conflicting-source handling is not scored. Treat the
numbers as a regression gate and a way to compare changes, not as a certification.
