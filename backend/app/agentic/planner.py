from .types import AgentPlan, AgentStep

class BoundedPlanner:
    """
    Deterministic orchestration policy around LLM-powered components.

    The planner itself is intentionally bounded. It cannot recursively call
    tools without limits.
    """
    def __init__(self, max_retrieval_calls=2, max_subqueries=3):
        self.max_retrieval_calls = max_retrieval_calls
        self.max_subqueries = max_subqueries

    def plan(self, query: str, complex_query: bool = False) -> AgentPlan:
        steps = []
        if complex_query:
            steps.append(AgentStep(
                action="decompose",
                reason="Question appears to contain multiple independent information needs.",
                query=query,
            ))
        steps.append(AgentStep(
            action="retrieve",
            reason="Retrieve evidence before generation.",
            query=query,
        ))
        steps.append(AgentStep(
            action="verify",
            reason="Verify evidence/claims before final response.",
        ))
        steps.append(AgentStep(
            action="answer",
            reason="Generate only after evidence verification.",
        ))
        return AgentPlan(
            original_query=query,
            steps=steps,
            max_retrieval_calls=self.max_retrieval_calls,
            max_subqueries=self.max_subqueries,
        )
