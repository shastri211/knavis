from ..provider_service import ProviderService
from .evidence_gate import assess_evidence, build_evidence_packet
from .finalizer import finalize_answer
from .prompt import GROUNDING_SYSTEM_PROMPT

class GroundedAnswerService:
    def __init__(self, providers: ProviderService):
        self.providers = providers

    async def answer(
        self,
        query: str,
        evidence: list[dict],
        provider: str,
        model: str,
        language_hint: str | None = None,
    ):
        decision = assess_evidence(query, evidence)
        if not decision.sufficient:
            return {
                "answer": "I don't have enough reliable evidence in the provided material to answer that accurately, so I won't guess.",
                "citations": [],
                "grounded": False,
                "reason": decision.reason,
            }

        packet = build_evidence_packet(decision.selected)
        language = language_hint or "the user's language"
        prompt = (
            f"User language: {language}\n\n"
            f"EVIDENCE:\n{packet}\n\n"
            f"QUESTION:\n{query}\n\n"
            "Answer only from the evidence. Cite each factual claim."
        )

        response = await self.providers.chat(
            provider,
            model,
            [
                {"role": "system", "content": GROUNDING_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=1600,
        )

        answer, citations, grounded = finalize_answer(
            response.text, decision.selected, require_citations=True
        )

        return {
            "answer": answer,
            "citations": citations,
            "grounded": grounded,
            "reason": "verified" if grounded else "citation/evidence validation failed",
            "usage": response.usage,
        }
