from app.grounding.evidence_gate import assess_evidence
from app.retrieval.bm25 import BM25Index
from types import SimpleNamespace
from app.retrieval.rrf import reciprocal_rank_fusion
from app.retrieval.text import content_terms, is_overview_query, lexical_coverage, tokenize

DOC = "Data Retention Policy. Company data must be retained for 90 days. Backups are kept for 30 days."
QUESTION = "How long must company data be retained?"


def _fused(texts):
    chunks = [SimpleNamespace(id=f"d:{i}", text=t, metadata={"source": "p.txt"}) for i, t in enumerate(texts)]
    index = BM25Index(); index.build(chunks)
    return reciprocal_rank_fusion([[], index.search(QUESTION, 12)], limit=12)


def test_single_chunk_corpus_is_sufficient_although_bm25_is_not_positive():
    candidates = _fused([DOC])
    assert candidates[0]["bm25_score"] <= 0  # the failure mode this guards against
    assert assess_evidence(QUESTION, candidates).sufficient


def test_irrelevant_candidates_are_rejected():
    candidates = _fused([DOC, "Employees must badge in at the front door.", "The cafeteria opens at 8."])
    assert not assess_evidence("Who won the football world cup in 1998?", candidates).sufficient


def test_dense_score_alone_can_pass_for_cross_language_matches():
    item = {"id": "a", "text": "Company data must be retained for 90 days.", "dense_score": 0.6}
    assert assess_evidence("डेटा कितने दिन रखना है?", [item]).sufficient
    assert not assess_evidence("डेटा कितने दिन रखना है?", [{**item, "dense_score": 0.1}]).sufficient


def test_rrf_keeps_both_signals_when_a_chunk_is_found_by_both_retrievers():
    """Regression: the sparse copy used to overwrite the dense score (0.62 -> 0.2)."""
    dense = [{"id": "a", "text": "x", "dense_score": 0.62, "score": 0.62}]
    sparse = [{"id": "a", "text": "x", "bm25_score": 0.2, "score": 0.2}]
    fused = reciprocal_rank_fusion([dense, sparse])[0]
    assert fused["dense_score"] == 0.62 and fused["bm25_score"] == 0.2


def test_selection_is_capped_and_ordered_by_fused_rank():
    items = [{"id": str(i), "text": DOC, "rrf_score": i / 100} for i in range(20)]
    selected = assess_evidence(QUESTION, items, max_items=5).selected
    assert len(selected) == 5 and selected[0]["id"] == "19"


def test_tokenizer_keeps_hindi_words_whole():
    assert tokenize("कैसे हो?") == ["कैसे", "हो"]
    assert tokenize("Retained, for 90-days!") == ["retained", "for", "90", "days"]


def test_content_terms_drop_stopwords_and_light_stemming_matches_inflections():
    assert content_terms("What is the retention period for audit logs?") == ["retention", "period", "audit", "log"]
    coverage, matched, total = lexical_coverage("audit logging", "Audit logs are kept")
    assert (matched, total) == (1, 2) and coverage == 0.5


def test_overview_detection_does_not_swallow_normal_questions():
    for q in ("Summarize this", "give me an overview", "What is this document about?", "what does the file contain", "What does this PDF say?"):
        assert is_overview_query(q), q
    for q in ("What is the document retention policy?", "How long are logs retained?", "who is the author of section 4"):
        assert not is_overview_query(q), q
