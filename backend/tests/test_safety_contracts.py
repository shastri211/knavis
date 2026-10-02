from app.evaluation.safety_cases import CASES

def test_safety_suite_exists():
    assert len(CASES) >= 4
    assert any(c.name == "prompt_injection_document" for c in CASES)
    assert any(c.name == "missing_evidence" for c in CASES)
