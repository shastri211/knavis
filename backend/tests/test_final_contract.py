from app.finalization.healthcheck import project_contract

def test_core_project_modules_import():
    result = project_contract()
    assert result["agentic"] == "ok"
    assert result["grounding"] == "ok"
    assert result["retrieval"] == "ok"
    assert result["reliability"] == "ok"
