"""Verification behaviours found by running real documents through a real LLM.

Each case is a shape of answer that models actually produce and that an earlier, stricter verifier rejected.
"""
from app.agentic.verification import trim_unsupported, verify_answer
from conftest import FACT, make_txt

PROBLEM = {"text": "Easy: Food Stamps. There are n food types and you may eat at most m meals in total."}
CONSTRAINTS = {"text": "Constraints\n1 <= n <= 10^5\n1 <= m <= 10^9\n1 <= v[i] <= 10^9"}
EVIDENCE = [PROBLEM, CONSTRAINTS]


def test_a_formula_labelled_with_words_from_a_neighbouring_chunk_is_supported():
    answer = ("- Number of food types: \\(1 \\leq n \\leq 10^5\\)【EVIDENCE 2】\n"
              "- Maximum number of meals: \\(1 \\leq m \\leq 10^9\\)【EVIDENCE 2】")
    result = verify_answer(answer, EVIDENCE)
    assert result.supported          # LaTeX markup is ignored and the numbers are all in the cited chunk


def test_a_wrong_citation_number_is_repaired_when_the_fact_is_in_another_chunk():
    evidence = [{"text": "Company data must be retained for 90 days after the contract ends."}, {"text": "The cafeteria opens at 8 and closes at 3."}]
    result = verify_answer("Company data must be retained for 90 days after the contract ends [EVIDENCE 2].", evidence)
    assert result.supported and result.claims[0].evidence_ids == [1] and result.claims[0].attributed


def test_the_second_chance_never_rescues_an_invented_number_or_unrelated_claim():
    evidence = [{"text": "Company data must be retained for 90 days after the contract ends."}, {"text": "The cafeteria opens at 8 and closes at 3."}]
    assert not verify_answer("Company data must be retained for 7 years [EVIDENCE 2].", evidence).supported
    assert not verify_answer("The cafeteria serves free breakfast to all visitors every morning [EVIDENCE 1].", evidence).supported


GIT = [{"text": "Git tracks every change made to a project and lets you recover earlier versions of your work."},
       {"text": "GitHub hosts Git repositories online so teams can share and collaborate on code."},
       {"text": "A commit saves a checkpoint of your project that you can return to later."},
       {"text": "Pushing sends your commits from your computer to GitHub."}]
SUMMARY = ("Git tracks every change made to a project and lets you recover earlier versions of your work [EVIDENCE 1].\n"
           "GitHub hosts Git repositories online so teams can share and collaborate on code [EVIDENCE 2].\n"
           "A commit saves a checkpoint of your project that you can return to later [EVIDENCE 3].\n"
           "Pushing sends your commits from your computer to GitHub [EVIDENCE 4].\n"
           "Git also compiles your program automatically and deploys it to every customer server worldwide [EVIDENCE 1].")


def test_one_unsupported_sentence_in_a_long_correct_answer_is_cut_out_not_the_whole_answer():
    result = verify_answer(SUMMARY, GIT)
    assert not result.supported and len(result.rejected) == 1 and result.checked == 5
    trimmed = trim_unsupported(SUMMARY, result)
    assert "deploys it to every customer" not in trimmed and "Pushing sends your commits" in trimmed
    assert verify_answer(trimmed, GIT).supported                             # what remains is fully supported


def test_when_most_of_an_answer_is_unsupported_it_is_rejected_whole():
    answer = ("Git tracks every change made to a project [EVIDENCE 1].\n"
              "Git compiles programs and deploys them to every customer server worldwide [EVIDENCE 1].\n"
              "Git also encrypts every repository with military grade quantum keys [EVIDENCE 1].\n"
              "GitHub employs ten thousand engineers across seven continents [EVIDENCE 2].")
    result = verify_answer(answer, GIT)
    assert not result.supported and trim_unsupported(answer, result) is None


def test_a_single_unsupported_sentence_is_never_trimmed_to_an_empty_answer():
    result = verify_answer("Git compiles programs and deploys them to every customer server worldwide [EVIDENCE 1].", GIT)
    assert not result.supported and trim_unsupported("Git compiles programs and deploys them to every customer server worldwide [EVIDENCE 1].", result) is None


def test_the_user_is_told_when_a_sentence_was_left_out(llm, session_id, upload, ask):
    """End to end: the answer is delivered without the unsupported sentence, with a visible note and real citations."""
    upload(session_id, "policy.txt", make_txt(), "text/plain")
    llm.answer = (f"{FACT} [EVIDENCE 1]\n"
                  "Backups are kept for 30 days [EVIDENCE 1].\n"
                  "Audit logs are retained for 365 days [EVIDENCE 1].\n"
                  "Deletion requests are answered within 14 days [EVIDENCE 1].\n"
                  "All of this is enforced by an independent regulator that fines violators heavily [EVIDENCE 1].")
    result = ask(session_id, "What are the retention rules?")
    text = result["message"]["content"]
    assert "regulator" not in text and "Backups are kept for 30 days" in text
    assert "1 statement could not be verified" in text and result["citations"]
