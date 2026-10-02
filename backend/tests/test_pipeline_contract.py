from app.integration.pipeline import IntegratedRAGPipeline

def test_collection_name_is_safe():
    p = IntegratedRAGPipeline()
    assert p.collection("a/b:c") == "session_a_b_c"
