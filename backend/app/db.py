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
def init_db():
 from .models import ChatSession,Message,Document,Evidence,Job,UsageEvent,DocChunk,ExtractionCache,EmbeddingCache,QuotaUsage,SpecialistCache
 Base.metadata.create_all(engine)
