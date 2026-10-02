# Evaluation

Run the harness from `backend`:

```bash
python -m app.evaluation.run
```

The dataset deliberately mixes:
- English
- Hindi
- Hinglish
- mixed-language queries
- greetings/normal conversation
- unsupported questions
- prompt injection
- evidence conflicts

## Metrics

### Routing accuracy
Did the semantic router choose the correct path?

### Grounding rate
For questions requiring documents, did the system produce a grounded result
instead of unsupported general knowledge?

### Safe abstention rate
For cases that should not be answered, did the system refuse/abstain safely?

### Citation validity
Did every citation refer to real retrieved evidence?

## What this does NOT measure automatically

Exact answer quality cannot be honestly reduced to string matching.

For serious evaluation, annotate:
- expected answer
- acceptable paraphrases
- evidence span IDs
- source authority
- conflict status

Then add human or model-assisted claim-level grading.

## Minimum target before demo

Do not tune the system until you have at least:
- 30 normal RAG questions
- 10 Hindi
- 10 Hinglish
- 10 mixed-language
- 10 out-of-scope
- 10 conversation
- 10 adversarial/prompt-injection
- 5 conflicting-source cases
- 5 scanned-document cases
- 5 table cases
- 5 audio cases

This is a starting benchmark, not a certification.
