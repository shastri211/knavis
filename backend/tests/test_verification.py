from app.agentic.conflicts import detect_numeric_conflicts
from app.agentic.verification import extract_claims, verify_answer
from app.grounding.citations import extract_citation_ids, source_citations

EVIDENCE = [{"text": "Company data must be retained for 90 days. Backups are kept for 30 days."}]


def test_cited_answer_passes_even_when_evidence_has_several_numbers():
    assert verify_answer("Company data must be retained for 90 days [EVIDENCE 1].", EVIDENCE).supported


def test_number_missing_from_cited_evidence_fails():
    result = verify_answer("Company data must be retained for 7 days [EVIDENCE 1].", EVIDENCE)
    assert not result.supported and result.unsupported_claims


def test_thousands_separators_and_non_ascii_digits_are_normalised():
    evidence = [{"text": "The fee is 1,200 rupees. प्रोसेसिंग में १४ दिन लगते हैं।"}]
    assert verify_answer("The fee is 1200 rupees [EVIDENCE 1].", evidence).supported
    assert verify_answer("Processing takes 14 days, the document states [EVIDENCE 1].", evidence).supported


def test_an_uncited_sentence_is_attributed_to_the_evidence_that_contains_it_or_rejected():
    supported = verify_answer("Company data must be retained for 90 days.", EVIDENCE)
    assert supported.supported and supported.claims[0].attributed and supported.claims[0].evidence_ids == [1]
    assert not verify_answer("Company data must be retained for 7 years.", EVIDENCE).supported        # the number is not there
    assert not verify_answer("The cafeteria serves vegetarian meals every single day.", EVIDENCE).supported   # nothing supports it


def test_one_citation_at_the_end_of_a_line_covers_the_sentences_before_it():
    """Models cite a paragraph once, at its end."""
    answer = "Company data must be retained for 90 days. Backups are kept for 30 days. Both rules apply [EVIDENCE 1]."
    result = verify_answer(answer, EVIDENCE)
    assert result.supported and all(c.evidence_ids == [1] for c in result.claims) and not any(c.attributed for c in result.claims)


def test_an_invented_sentence_on_the_same_line_does_not_borrow_the_citation():
    answer = "Company data must be retained for 90 days [EVIDENCE 1]. The retention period is mandated by European law."
    assert not verify_answer(answer, EVIDENCE).supported


def test_uncited_factual_sentence_fails_but_courtesy_and_lead_ins_do_not():
    assert verify_answer("Company data must be retained for 90 days [EVIDENCE 1]. Let me know if you need more.", EVIDENCE).supported
    assert verify_answer("The policy lists these rules:\n- Company data must be retained for 90 days [EVIDENCE 1].", EVIDENCE).supported


def test_citation_on_its_own_after_the_period_attaches_to_the_previous_sentence():
    claims = extract_claims("Company data must be retained for 90 days. [EVIDENCE 1]")
    assert len(claims) == 1 and claims[0].evidence_ids == [1]


def test_nonexistent_evidence_number_fails():
    assert not verify_answer("Company data must be retained for 90 days [EVIDENCE 7].", EVIDENCE).supported


def test_claim_unrelated_to_its_cited_evidence_fails():
    assert not verify_answer("The cafeteria serves vegetarian meals every single day [EVIDENCE 1].", EVIDENCE).supported


def test_empty_answer_is_not_supported():
    assert not verify_answer("", EVIDENCE).supported


def test_citation_formats_models_commonly_produce():
    assert extract_citation_ids("a [EVIDENCE 1] b [evidence 2 | file.pdf, page 3] c [EVIDENCE 1, EVIDENCE 3]") == [1, 2, 1, 3]
    assert extract_citation_ids("[EVIDENCE 4][EVIDENCE 5]") == [4, 5]


def test_citation_records_keep_the_number_the_model_used():
    evidence = [{"id": "a", "metadata": {"source": "x.txt"}}, {"id": "b", "metadata": {"source": "y.txt", "page": 2}}]
    records = source_citations(evidence, [2])
    assert records[0]["evidence_id"] == 2 and records[0]["source"] == "y.txt" and records[0]["page"] == 2


def test_same_document_numbers_are_not_a_conflict_but_cross_document_differences_are():
    same_doc = [{"text": "Keep data 90 days.", "metadata": {"document_id": "A"}},
                {"text": "Keep backups 30 days.", "metadata": {"document_id": "A"}}]
    assert detect_numeric_conflicts(same_doc) == []
    cross_doc = [{"text": "Keep data 90 days.", "metadata": {"document_id": "A"}},
                 {"text": "Keep data 30 days.", "metadata": {"document_id": "B"}}]
    conflicts = detect_numeric_conflicts(cross_doc)
    assert len(conflicts) == 1 and conflicts[0].evidence_ids == [1, 2]
    # a conflict is reported, never a reason on its own to reject the answer
    assert verify_answer("Keep data 90 days [EVIDENCE 1].", cross_doc).supported


def test_citations_written_with_fullwidth_or_round_brackets_are_understood():
    """gpt-oss writes 【EVIDENCE 1】; discarding such an answer would throw away a correct one."""
    assert extract_citation_ids("Retained for **90 days**【EVIDENCE 1】. Backups too (EVIDENCE 2).") == [1, 2]
    assert verify_answer("Company data must be retained for 90 days【EVIDENCE 1】.", EVIDENCE).supported
