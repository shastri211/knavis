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

What this does **not** prove: string matching cannot grade wording, the corpus is small and synthetic, scanned pages
and audio are not covered (they need paid OCR/transcription), and conflicting-source handling is not scored. Treat the
numbers as a regression gate and a way to compare changes, not as a certification.
