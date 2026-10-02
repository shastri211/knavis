from app.agentic.decomposer import decompose_query
from app.agentic.planner import BoundedPlanner
from app.agentic.verification import verify_answer

def test_decomposition_is_bounded():
    q = "What is retention and what is consent and what is deletion and what is access?"
    assert len(decompose_query(q, 3)) <= 3

def test_planner_has_hard_retrieval_limit():
    p = BoundedPlanner(max_retrieval_calls=2).plan("question")
    assert p.max_retrieval_calls == 2

def test_invalid_citation_fails_verification():
    r = verify_answer("Answer [EVIDENCE 7].", [{"text":"x"}])
    assert not r.supported
