import json
from dataclasses import dataclass
from datetime import datetime
from ..provider_service import ProviderService

@dataclass
class RouteDecision:
    intent: str; route: str; language: str; confidence: float; reason: str = ""
ALLOWED_INTENTS={"GREETING","HOW_ARE_YOU","IDENTITY","CAPABILITIES","THANKS","FAREWELL","DATE","TIME","DAY","NORMAL_CONVERSATION","RAG_QUERY","OUT_OF_SCOPE"}
ROUTER_SYSTEM='''You are the semantic router for a multilingual multimodal RAG application. Classify the user's message; do NOT answer it. Return ONLY JSON with keys intent, route, language, confidence, reason. Allowed intents: GREETING, HOW_ARE_YOU, IDENTITY, CAPABILITIES, THANKS, FAREWELL, DATE, TIME, DAY, NORMAL_CONVERSATION, RAG_QUERY, OUT_OF_SCOPE. Allowed routes: conversation, utility, rag, out_of_scope. For mixed Hindi-English use Hinglish. Greetings/social chat are NOT RAG. Current date/time/day are utility. Questions requiring uploaded documents, files, policies, reports, provided evidence, or corpus facts are RAG_QUERY. If ambiguous, prefer normal conversation unless there is a clear evidence/knowledge request.'''
class SemanticRouter:
    def __init__(self,providers:ProviderService): self.providers=providers
    async def classify(self,text,provider,model):
        # Handle cheap, unambiguous cases locally. This is intentionally a
        # fallback, not an English-only router: the provider classifier remains
        # responsible for ambiguous and multilingual conversational turns.
        local = self._deterministic(text)
        if local:
            return local
        try:
            r=await self.providers.chat(provider,model,[{"role":"system","content":ROUTER_SYSTEM},{"role":"user","content":text}],temperature=0,max_tokens=800,response_format={"type":"json_object"})
        except Exception:
            # Uncertain requests must not become general-knowledge answers.
            return RouteDecision("RAG_QUERY", "rag", "unknown", 0, "provider routing unavailable")
        try: d=json.loads(r.text)
        except json.JSONDecodeError: return RouteDecision("OUT_OF_SCOPE","out_of_scope","unknown",0,"invalid router output")
        i=str(d.get("intent","OUT_OF_SCOPE")).upper(); rt=str(d.get("route","out_of_scope")).lower(); lang=str(d.get("language","unknown"))
        try: c=max(0,min(1,float(d.get("confidence",0))))
        except (TypeError,ValueError): c=0
        if i not in ALLOWED_INTENTS or rt not in {"conversation","utility","rag","out_of_scope"}: return RouteDecision("OUT_OF_SCOPE","out_of_scope",lang,0,"invalid router labels")
        return RouteDecision(i,rt,lang,c,str(d.get("reason","")))

    @staticmethod
    def _deterministic(text):
        normalized = " ".join(text.casefold().strip().split())
        if normalized in {"hi", "hello", "hey", "namaste", "नमस्ते", "हाय", "हेलो"}:
            return RouteDecision("GREETING", "conversation", "unknown", 1, "deterministic greeting")
        if normalized in {"how are you", "how are you?", "kaise ho", "kaise ho?", "कैसे हो", "कैसे हो?"}:
            return RouteDecision("HOW_ARE_YOU", "conversation", "unknown", 1, "deterministic conversation")
        if normalized in {"who are you", "who are you?", "aap kaun ho", "aap kaun ho?", "आप कौन हो", "आप कौन हो?"}:
            return RouteDecision("IDENTITY", "conversation", "unknown", 1, "deterministic identity")
        if normalized in {"thanks", "thank you", "shukriya", "धन्यवाद"}:
            return RouteDecision("THANKS", "conversation", "unknown", 1, "deterministic thanks")
        if normalized in {"bye", "goodbye", "alvida", "अलविदा"}:
            return RouteDecision("FAREWELL", "conversation", "unknown", 1, "deterministic farewell")
        if any(phrase in normalized for phrase in ("what time", "current time", "time is it", "kitne baje", "कितने बजे")):
            return RouteDecision("TIME", "utility", "unknown", 1, "deterministic time")
        if any(phrase in normalized for phrase in ("what date", "today's date", "aaj ki date", "आज की तारीख")):
            return RouteDecision("DATE", "utility", "unknown", 1, "deterministic date")
        if any(phrase in normalized for phrase in ("what day", "which day", "kaun sa din", "कौन सा दिन")):
            return RouteDecision("DAY", "utility", "unknown", 1, "deterministic day")
        return None
def utility_answer(intent):
    n=datetime.now().astimezone()
    if intent=="DATE": return f"Today's date is {n.strftime('%B %d, %Y')}."
    if intent=="TIME": return f"The current time is {n.strftime('%I:%M %p')} {n.tzname() or ''}.".strip()
    if intent=="DAY": return f"Today is {n.strftime('%A')}."
CONVERSATION_RESPONSES={"GREETING":"Hello! How can I help you?","HOW_ARE_YOU":"I'm doing well and ready to help. What would you like to work on?","IDENTITY":"I'm a multilingual multimodal Agentic RAG assistant. I can work with supported documents and answer evidence-grounded questions.","CAPABILITIES":"I can process supported documents, retrieve relevant evidence, and answer grounded questions with citations. Ordinary conversation is handled separately from RAG.","THANKS":"You're welcome!","FAREWELL":"Goodbye!"}
