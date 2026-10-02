from datetime import datetime,timezone
from uuid import uuid4
from sqlalchemy import String,Text,DateTime,ForeignKey,JSON,Integer,Float
from sqlalchemy.orm import Mapped,mapped_column,relationship
from .db import Base
def now(): return datetime.now(timezone.utc)
class ChatSession(Base):
 __tablename__='sessions'; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4())); title:Mapped[str]=mapped_column(String(200),default='New chat'); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now); messages=relationship('Message',cascade='all,delete-orphan',back_populates='session'); documents=relationship('Document',cascade='all,delete-orphan',back_populates='session')
class Message(Base):
 __tablename__='messages'; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4())); session_id:Mapped[str]=mapped_column(ForeignKey('sessions.id'),index=True); role:Mapped[str]=mapped_column(String(20)); content:Mapped[str]=mapped_column(Text); language:Mapped[str|None]=mapped_column(String(50),nullable=True); intent:Mapped[str|None]=mapped_column(String(60),nullable=True); provider:Mapped[str|None]=mapped_column(String(40),nullable=True); model:Mapped[str|None]=mapped_column(String(200),nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); session=relationship('ChatSession',back_populates='messages')
class Document(Base):
 __tablename__='documents'; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4())); session_id:Mapped[str]=mapped_column(ForeignKey('sessions.id'),index=True); filename:Mapped[str]=mapped_column(String(255)); content_type:Mapped[str]=mapped_column(String(120)); path:Mapped[str]=mapped_column(Text); status:Mapped[str]=mapped_column(String(40),default='uploaded'); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); metadata_json:Mapped[dict|None]=mapped_column(JSON,nullable=True); session=relationship('ChatSession',back_populates='documents'); evidence=relationship('Evidence',cascade='all,delete-orphan',back_populates='document')
class Evidence(Base):
 __tablename__='evidence'; id:Mapped[str]=mapped_column(String(120),primary_key=True); document_id:Mapped[str]=mapped_column(ForeignKey('documents.id'),index=True); kind:Mapped[str]=mapped_column(String(40)); text:Mapped[str]=mapped_column(Text); page:Mapped[int|None]=mapped_column(Integer,nullable=True); metadata_json:Mapped[dict|None]=mapped_column(JSON,nullable=True); document=relationship('Document',back_populates='evidence')


class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), index=True)
    document_id: Mapped[str|None] = mapped_column(ForeignKey('documents.id'), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(40), default='ingestion')
    status: Mapped[str] = mapped_column(String(30), default='queued')
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(80), default='queued')
    error: Mapped[str|None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class UsageEvent(Base):
    __tablename__ = 'usage_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey('sessions.id'), index=True)
    message_id: Mapped[str|None] = mapped_column(ForeignKey('messages.id'), nullable=True)
    provider: Mapped[str|None] = mapped_column(String(40), nullable=True)
    model: Mapped[str|None] = mapped_column(String(200), nullable=True)
    input_tokens: Mapped[int|None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int|None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int|None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
