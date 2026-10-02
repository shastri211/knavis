from .contracts import AgentRequest, AgentResponse

class GreetingAgent:
    """
    Handles ordinary conversational turns that should never invoke retrieval.

    It uses the existing semantic router for intent classification and the
    selected LLM only when a fixed response is not sufficient.
    """
    FIXED = {
        "GREETING": "Hello! How can I help you?",
        "HOW_ARE_YOU": "I'm doing well and ready to help. What would you like to work on?",
        "THANKS": "You're welcome!",
        "FAREWELL": "Goodbye! Take care.",
        "IDENTITY": "I'm a document-grounded AI assistant. I can chat normally and answer questions from the documents you provide.",
        "CAPABILITIES": "I can handle normal conversation and perform evidence-grounded questions over supported documents, including multilingual queries.",
    }

    def __init__(self, router, provider_service):
        self.router = router
        self.provider_service = provider_service

    async def handle(self, request: AgentRequest, decision=None) -> AgentResponse:
        decision = decision or await self.router.classify(
            request.text, request.provider, request.model
        )
        intent = decision.intent

        if intent in self.FIXED:
            return AgentResponse(
                text=self.FIXED[intent],
                route="greeting",
                metadata={"intent": intent, "language": decision.language},
            )

        response = await self.provider_service.chat(
            request.provider,
            request.model,
            [
                {
                    "role": "system",
                    "content": (
                        "You are the Conversation Agent of a document-grounded AI "
                        "system. Handle ordinary conversation naturally. Do not "
                        "answer document-specific knowledge questions. Do not invent "
                        "document facts. Keep the response concise."
                    ),
                },
                {"role": "user", "content": request.text},
            ],
            temperature=0.4,
            max_tokens=500,
        )
        return AgentResponse(
            text=response.text,
            route="greeting",
            metadata={
                "intent": intent,
                "language": decision.language,
                "usage": response.usage,
            },
        )
