import importlib

REQUIRED_MODULES = [
    "fastapi",
    "pydantic",
    "sqlalchemy",
    "httpx",
    "fitz",
    "qdrant_client",
]

def dependency_report():
    result = {}
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
            result[name] = "ok"
        except Exception as exc:
            result[name] = f"missing: {exc}"
    return result

def project_contract():
    checks = {
        "agentic": "app.agentic.runner",
        "grounding": "app.grounding.answer_service",
        "retrieval": "app.retrieval.rrf",
        "multimodal": "app.multimodal.assemblyai",
        "document_ai": "app.document_ai.pdf_bundle",
        "reliability": "app.reliability.circuit_breaker",
    }
    result = {}
    for label, module in checks.items():
        try:
            importlib.import_module(module)
            result[label] = "ok"
        except Exception as exc:
            result[label] = f"broken: {exc}"
    return result
