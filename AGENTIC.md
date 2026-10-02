# Phase 8 — Bounded Agentic RAG

## Why this is agentic

The system can now decide whether to:
- decompose a multi-part query,
- retrieve,
- retry with a rewritten retrieval query,
- verify,
- answer,
- abstain.

It is NOT an unconstrained autonomous loop.

## Hard limits

Default:
- maximum retrieval calls: 2
- maximum subqueries: 3

These are software limits, not prompt instructions.

## Query decomposition

Explicit multi-part questions are conservatively split.

Example:

"What is the retention period and what are the consent requirements?"

becomes at most:
1. retention period
2. consent requirements

The final answer still has to be grounded in retrieved evidence.

## Retrieval retry

If initial retrieval is empty, a conservative rewrite can be attempted once.

The rewrite does not add facts. It only adds retrieval-oriented wording.

## Verification

The verification layer currently checks:
- every factual sentence has an evidence marker,
- every marker points to an existing evidence item,
- obvious numeric conflicts in the retrieved evidence are flagged.

This is a safety layer, not a semantic theorem prover.

A future phase should benchmark a dedicated multilingual entailment/claim-verification
model. Until then, the application must continue to use conservative abstention.

## Conflict handling

If retrieved evidence contains conflicting numeric values, the system does not
silently choose one. It flags the conflict and abstains from the generated answer.

A production conflict resolver should later reason over source authority, dates,
document versions and logical-document boundaries.

## Tool budget

The agent has no permission to:
- loop indefinitely,
- repeatedly call the LLM,
- retrieve the entire corpus,
- bypass the evidence gate,
- bypass citation validation.

## Next

Phase 9 should connect the agent to:
- chat/session state,
- background ingestion jobs,
- multimodal evidence,
- token/cost accounting,
- model registry,
- guardrails,
- authentication,
- observability,
- frontend agent status.
