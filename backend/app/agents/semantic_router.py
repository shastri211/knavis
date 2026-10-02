import json
from dataclasses import dataclass
from datetime import datetime
from ..provider_service import ProviderService
from ..retrieval.text import tokenize

@dataclass
class RouteDecision:
    intent: str; route: str; language: str; confidence: float; reason: str = ""
ALLOWED_INTENTS={"GREETING","HOW_ARE_YOU","IDENTITY","CAPABILITIES","THANKS","FAREWELL","DATE","TIME","DAY","NORMAL_CONVERSATION","RAG_QUERY","OUT_OF_SCOPE"}
ROUTER_SYSTEM='''You are the semantic router for a multilingual multimodal RAG application. Classify the user's message; do NOT answer it. Return ONLY JSON with keys intent, route, language, confidence, reason. Allowed intents: GREETING, HOW_ARE_YOU, IDENTITY, CAPABILITIES, THANKS, FAREWELL, DATE, TIME, DAY, NORMAL_CONVERSATION, RAG_QUERY, OUT_OF_SCOPE. Allowed routes: conversation, utility, rag, out_of_scope. For mixed Hindi-English use Hinglish. Greetings/social chat are NOT RAG. Current date/time/day are utility. Questions requiring uploaded documents, files, policies, reports, provided evidence, or corpus facts are RAG_QUERY. If ambiguous, prefer normal conversation unless there is a clear evidence/knowledge request.'''
_GREETING_WORDS = {"hi", "hii", "hiii", "hello", "hey", "heya", "hola", "namaste", "namaskar", "नमस्ते", "नमस्कार", "हाय", "हेलो", "हैलो",
                   "pranam", "प्रणाम"}
_PERIODS = {"morning", "afternoon", "evening", "night"}
_SOCIAL = {
    "HOW_ARE_YOU": {"how are you", "how are you doing", "how r u", "how do you do", "kaise ho", "kaisa hai", "kaise hain aap",
                    "aap kaise ho", "कैसे हो", "आप कैसे हैं", "आप कैसे हो"},
    "IDENTITY": {"who are you", "what are you", "what is your name", "whats your name", "aap kaun ho", "tum kaun ho",
                 "आप कौन हो", "आप कौन हैं", "तुम कौन हो"},
    "CAPABILITIES": {"what can you do", "what do you do", "how can you help", "how can you help me", "help", "help me",
                     "what are your capabilities", "aap kya kar sakte ho", "tum kya kar sakte ho", "आप क्या कर सकते हैं"},
    "THANKS": {"thanks", "thank you", "thanks a lot", "thank you so much", "thanks so much", "ok thanks", "okay thanks",
               "thx", "ty", "shukriya", "shukriya ji", "dhanyavaad", "dhanyavad", "धन्यवाद", "शुक्रिया"},
    "FAREWELL": {"bye", "bye bye", "goodbye", "good bye", "see you", "see you later", "take care", "alvida", "अलविदा"},
}
# A request is a utility only when it contains nothing but the trigger and filler words, so "what time did the
# meeting start" is still a document question.
_UTILITIES = (
    ("TIME", ("what time", "current time", "time is it", "the time", "kitne baje", "कितने बजे"),
     {"what", "whats", "the", "time", "is", "it", "current", "now", "right", "please", "tell", "me", "kitne", "baje", "hai",
      "abhi", "कितने", "बजे", "है", "अभी", "क्या", "s"}),
    ("DATE", ("what date", "todays date", "today s date", "date today", "current date", "the date", "aaj ki date", "आज की तारीख"),
     {"what", "whats", "is", "the", "date", "today", "s", "todays", "current", "please", "tell", "me", "aaj", "ki", "hai",
      "आज", "की", "तारीख", "है", "क्या"}),
    ("DAY", ("what day", "which day", "kaun sa din", "कौन सा दिन"),
     {"what", "whats", "which", "day", "is", "it", "today", "the", "of", "week", "please", "tell", "me", "aaj", "kaun", "sa",
      "din", "hai", "आज", "कौन", "सा", "दिन", "है", "क्या"}),
)
_GREETING_FILLER = {"to", "you", "all", "everyone", "there", "sir", "madam", "ji", "team", "friend", "dear"}


class SemanticRouter:
    """Decides what kind of turn this is.

    Greetings, thanks, "what can you do" and the date/time are recognised by rules and cost nothing. Once the
    session has documents every other message is a grounded document question, which needs no routing call
    either (the evidence gate abstains when the documents do not cover it), so a normal question costs exactly
    one model call: the answer. Only a session WITHOUT documents still asks the model to tell a chat message
    from a question about documents that were never uploaded.
    """
    def __init__(self,providers:ProviderService): self.providers=providers
    async def classify(self,text,provider,model,has_documents=False):
        local = self._deterministic(text)
        if local:
            return local
        if has_documents:
            return RouteDecision("RAG_QUERY", "rag", "unknown", 1, "session has documents: grounded retrieval")
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
        tokens = tokenize(text)
        if not tokens or len(tokens) > 8:
            return None
        normalized = " ".join(tokens)
        padded = f" {normalized} "
        for intent, triggers, filler in _UTILITIES:
            if any(f" {t} " in padded for t in triggers) and set(tokens) <= filler:
                return RouteDecision(intent, "utility", "unknown", 1, f"deterministic {intent.lower()}")
        # "hi", "hello there", "good morning", "good morning to you", "namaste ji"
        greeted = tokens[0] in _GREETING_WORDS and set(tokens[1:]) <= (_GREETING_FILLER | _GREETING_WORDS)
        timed = tokens[0] == "good" and len(tokens) > 1 and tokens[1] in _PERIODS and set(tokens[2:]) <= _GREETING_FILLER
        if greeted or timed:
            return RouteDecision("GREETING", "conversation", "unknown", 1, "deterministic greeting")
        for intent, phrases in _SOCIAL.items():
            if normalized in phrases:
                return RouteDecision(intent, "conversation", "unknown", 1, "deterministic conversation")
        return None
def utility_answer(intent):
    n=datetime.now().astimezone()
    if intent=="DATE": return f"Today's date is {n.strftime('%B %d, %Y')}."
    if intent=="TIME": return f"The current time is {n.strftime('%I:%M %p')} {n.tzname() or ''}.".strip()
    if intent=="DAY": return f"Today is {n.strftime('%A')}."
CONVERSATION_RESPONSES={"GREETING":"Hello! How can I help you?","HOW_ARE_YOU":"I'm doing well and ready to help. What would you like to work on?","IDENTITY":"I'm a multilingual multimodal Agentic RAG assistant. I can work with supported documents and answer evidence-grounded questions.","CAPABILITIES":"I can process supported documents, retrieve relevant evidence, and answer grounded questions with citations. Ordinary conversation is handled separately from RAG.","THANKS":"You're welcome!","FAREWELL":"Goodbye!"}
