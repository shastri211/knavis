from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase,sessionmaker
from .config import settings
class Base(DeclarativeBase): pass
engine=create_engine(f"sqlite:///{(settings.data_dir/'app.db').resolve()}",connect_args={'check_same_thread':False})
SessionLocal=sessionmaker(bind=engine,autoflush=False,autocommit=False)
def init_db():
 from .models import ChatSession,Message,Document,Evidence,Job,UsageEvent
 Base.metadata.create_all(engine)
