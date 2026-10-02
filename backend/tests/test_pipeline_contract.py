from app.integration.pipeline import IntegratedRAGPipeline


def test_every_session_shares_one_collection_and_the_old_per_session_name_is_safe():
    p = IntegratedRAGPipeline()
    assert p.collection_name == "knavis_chunks"
    assert p.legacy_collection("a/b:c") == "session_a_b_c"
