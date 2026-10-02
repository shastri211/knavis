from datetime import datetime,timezone
from uuid import uuid4
from sqlalchemy import String,Text,DateTime,ForeignKey,JSON,Integer,Float,LargeBinary
from sqlalchemy.orm import Mapped,mapped_column,relationship
from .db import Base
def now(): return datetime.now(timezone.utc)
class ChatSession(Base):
 __tablename__='sessions'; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4())); user_id:Mapped[str|None]=mapped_column(String(36),nullable=True,index=True); title:Mapped[str]=mapped_column(String(200),default='New chat'); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now); messages=relationship('Message',cascade='all,delete-orphan',back_populates='session'); documents=relationship('Document',cascade='all,delete-orphan',back_populates='session')
class Message(Base):
 __tablename__='messages'; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4())); session_id:Mapped[str]=mapped_column(ForeignKey('sessions.id'),index=True); role:Mapped[str]=mapped_column(String(20)); content:Mapped[str]=mapped_column(Text); language:Mapped[str|None]=mapped_column(String(50),nullable=True); intent:Mapped[str|None]=mapped_column(String(60),nullable=True); provider:Mapped[str|None]=mapped_column(String(40),nullable=True); model:Mapped[str|None]=mapped_column(String(200),nullable=True); citations:Mapped[list|None]=mapped_column(JSON,nullable=True); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); session=relationship('ChatSession',back_populates='messages')
class Document(Base):
 __tablename__='documents'; id:Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4())); session_id:Mapped[str]=mapped_column(ForeignKey('sessions.id'),index=True); filename:Mapped[str]=mapped_column(String(255)); content_type:Mapped[str]=mapped_column(String(120)); path:Mapped[str]=mapped_column(Text); status:Mapped[str]=mapped_column(String(40),default='uploaded'); created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now); metadata_json:Mapped[dict|None]=mapped_column(JSON,nullable=True); session=relationship('ChatSession',back_populates='documents'); evidence=relationship('Evidence',cascade='all,delete-orphan',back_populates='document'); chunks=relationship('DocChunk',cascade='all,delete-orphan',back_populates='document')
 'Extraction details shown to clients (page counts, OCR estimate, cache reuse, ...).'
 @property
 def details(self): return self.metadata_json
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
    mode: Mapped[str|None] = mapped_column(String(20), nullable=True)        # auto | confirmed | native_only
    attempts: Mapped[int|None] = mapped_column(Integer, nullable=True)       # times the job was started
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


class DocChunk(Base):
    """A retrievable chunk, produced once at ingestion (see app.ingest.chunker)."""
    __tablename__ = 'chunks'
    id: Mapped[str] = mapped_column(String(140), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locator: Mapped[str | None] = mapped_column(String(200), nullable=True)
    section: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    document = relationship('Document', back_populates='chunks')


class ExtractionCache(Base):
    """Extraction (and any OCR/ASR) output keyed by file content, reused when the same file is uploaded again."""
    __tablename__ = 'extraction_cache'
    id: Mapped[str] = mapped_column(String(140), primary_key=True)   # "<sha256>:<extractor version>"
    kind: Mapped[str] = mapped_column(String(40))
    elements_json: Mapped[list] = mapped_column(JSON)
    info_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EmbeddingCache(Base):
    """Embedding vectors keyed by model and text, so identical text is never embedded twice."""
    __tablename__ = 'embedding_cache'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)    # sha256(model + NUL + input type + NUL + text)
    model: Mapped[str] = mapped_column(String(200))
    dimension: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)               # float32
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class QuotaUsage(Base):
    """Calls/pages/audio-seconds spent against one provider limit in the current window (see reliability.governor)."""
    __tablename__ = 'quota_usage'
    id: Mapped[str] = mapped_column(String(120), primary_key=True)   # "<provider>:<unit>:<window>"
    bucket: Mapped[int] = mapped_column(Integer)                      # which minute/hour/day the count belongs to
    used: Mapped[int] = mapped_column(Integer, default=0)


class SpecialistCache(Base):
    """One paid specialist result (an OCR page, a described figure, a transcript), reusable across documents and retries."""
    __tablename__ = 'specialist_cache'
    id: Mapped[str] = mapped_column(String(200), primary_key=True)   # "ocr:<file sha>:<page>", "fig:<image sha>", "asr:<file sha>"
    provider: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DataTable(Base):
    """One spreadsheet sheet / CSV loaded as a queryable table (see app.analytics.tablestore)."""
    __tablename__ = 'data_tables'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sheet: Mapped[str | None] = mapped_column(String(255), nullable=True)
    table_name: Mapped[str] = mapped_column(String(120))              # the SQL name inside the session's table file
    columns_json: Mapped[list] = mapped_column(JSON)                   # [{name, original, type, values?, min?, max?}]
    sample_json: Mapped[list | None] = mapped_column(JSON, nullable=True)   # the first few rows, as text
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    first_row: Mapped[int | None] = mapped_column(Integer, nullable=True)   # real sheet row numbers of the data
    last_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuthToken(Base):
    """A sign-in. Only the SHA-256 of the bearer token is stored, so a copy of the database cannot be used to sign in."""
    __tablename__ = 'auth_tokens'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PasswordReset(Base):
    """A single-use, expiring password-reset link. Only the SHA-256 of the token is stored."""
    __tablename__ = 'password_resets'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
