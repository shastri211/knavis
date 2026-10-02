from .planner import BoundedPlanner
from .decomposer import decompose_query
from .query_rewrite import rewrite_query
from .verification import verify_answer

class AgentRunner:
    """
    Bounded agent loop.

    Maximum retrieval attempts and subqueries are hard limits. The runner
    never allows a generated answer to bypass verification.
    """
    def __init__(self, retrieval, answer_service, max_retrieval_calls=2):
        self.retrieval = retrieval
        self.answer_service = answer_service
        self.planner = BoundedPlanner(max_retrieval_calls=max_retrieval_calls)

    async def run(self, session_id, query, provider, model, language=None):
        plan = self.planner.plan(query)
        queries = decompose_query(query, plan.max_subqueries)

        all_evidence = []
        calls = 0

        for q in queries[:plan.max_subqueries]:
            if calls >= plan.max_retrieval_calls:
                break
            calls += 1
            evidence = await self.retrieval(session_id, q, 12)
            all_evidence.extend(evidence)

        # Deduplicate by source/chunk identity.
        unique = {}
        for item in all_evidence:
            unique[item["id"]] = item
        evidence = list(unique.values())

        if not evidence and calls < plan.max_retrieval_calls:
            rewritten = rewrite_query(query, "insufficient retrieval evidence")
            calls += 1
            evidence = await self.retrieval(session_id, rewritten, 12)

        if not evidence:
            return {
                "answer": "I don't have enough reliable evidence in the provided material to answer that accurately, so I won't guess.",
                "citations": [],
                "grounded": False,
                "agent": {"retrieval_calls": calls, "status": "abstained"},
            }

        result = await self.answer_service.answer(
            query, evidence, provider, model, language
        )

        if not result.get("grounded"):
            return {**result, "agent": {"retrieval_calls": calls, "status": "abstained"}}

        verification = verify_answer(result["answer"], evidence)
        if not verification.supported:
            return {
                "answer": "I found retrieved material, but I could not verify the generated answer against it reliably, so I won't guess.",
                "citations": [],
                "grounded": False,
                "agent": {
                    "retrieval_calls": calls,
                    "status": "verification_failed",
                    "unsupported_claims": verification.unsupported_claims,
                    "conflicts": verification.conflicts,
                },
            }

        return {
            **result,
            "agent": {
                "retrieval_calls": calls,
                "status": "verified",
                "claims": len(verification.claims),
            },
        }
