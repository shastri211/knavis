from datetime import datetime
from pydantic import BaseModel,Field
class SessionCreate(BaseModel): title:str='New chat'
class SessionUpdate(BaseModel): title:str=Field(min_length=1,max_length=200)
class SessionOut(BaseModel): id:str; title:str; created_at:datetime; model_config={'from_attributes':True}
class MessageCreate(BaseModel): session_id:str; content:str=Field(min_length=1,max_length=20000); provider:str|None=None; model:str|None=None
class MessageOut(BaseModel): id:str; role:str; content:str; language:str|None=None; intent:str|None=None; provider:str|None=None; model:str|None=None; created_at:datetime; model_config={'from_attributes':True}
class ChatResponse(BaseModel): message:MessageOut; route:str; citations:list[dict]=[]
class ModelOption(BaseModel): id:str; provider:str; name:str; category:str; modalities:list[str]; languages:str; status:str; selectable:bool
class DocumentOut(BaseModel): id:str; filename:str; content_type:str; status:str; details:dict|None=None; model_config={'from_attributes':True}

class IngestionResult(BaseModel):
    document_id: str
    detected_type: str
    evidence_nodes: int
    chunks: int
    specialist_required: bool
    status: str


class ProcessRequest(BaseModel):
    action: str = Field(pattern="^(confirm|retry|skip)$")
