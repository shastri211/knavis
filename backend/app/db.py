from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase,sessionmaker
from .config import settings
class Base(DeclarativeBase): pass
engine=create_engine(f"sqlite:///{(settings.data_dir/'app.db').resolve()}",connect_args={'check_same_thread':False})
@event.listens_for(engine,'connect')
def _sqlite_pragmas(dbapi_connection,_record):
    # WAL lets readers and the single writer proceed together; the timeout makes a writer wait
    # for a busy database instead of failing immediately with "database is locked".
    cursor=dbapi_connection.cursor(); cursor.execute('PRAGMA journal_mode=WAL'); cursor.execute('PRAGMA busy_timeout=15000'); cursor.close()
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)
def get_db():
    """FastAPI dependency: one database session per request."""
    s=SessionLocal()
    try: yield s
    finally: s.close()
def init_db():
 from .models import ChatSession,Message,Document,Evidence,Job,UsageEvent,DocChunk,ExtractionCache,EmbeddingCache,QuotaUsage,SpecialistCache,DataTable,User,AuthToken
 from .migrations import add_missing_columns
 from .retrieval import fts
 Base.metadata.create_all(engine)
 add_missing_columns(engine,Base.metadata)   # columns added to a model after a database was created
 with SessionLocal() as db:
  fts.ensure_table(db)
  fts.rebuild_if_stale(db)   # databases from before the keyword index existed
