from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
class Settings(BaseSettings):
    host:str='127.0.0.1'; port:int=8000; frontend_origin:str='http://127.0.0.1:5173'
    # Relative DATA_DIR values resolve against the project root, never the process CWD,
    # so the database, uploads and renders always land in one place.
    data_dir:Path=BACKEND_DIR/'data'; nvidia_api_key:str=''; groq_api_key:str=''; openrouter_api_key:str=''; assemblyai_api_key:str=''
    nvidia_base_url:str='https://integrate.api.nvidia.com/v1'; nvidia_ocr_base_url:str='https://ai.api.nvidia.com/v1/ocr'; groq_base_url:str='https://api.groq.com/openai/v1'; openrouter_base_url:str='https://openrouter.ai/api/v1'
    qdrant_url:str=''; qdrant_api_key:str=''; qdrant_collection:str=''; openrouter_model:str=''
    embedding_model:str='nvidia/llama-nemotron-embed-1b-v2'; embedding_dimensions:int|None=None
    default_provider:str='nvidia'; default_model:str='meta/llama-3.1-8b-instruct'; max_upload_mb:int=50; max_message_chars:int=20000; max_agent_retrieval_calls:int=2; top_k_rerank:int=12
    # Evidence gate. A chunk is usable when its dense cosine similarity reaches
    # evidence_min_dense OR it covers at least evidence_min_coverage of the question's
    # content terms. At most evidence_max_items chunks are sent to the LLM.
    evidence_min_dense:float=0.35; evidence_min_coverage:float=0.5; evidence_max_items:int=8
    model_config=SettingsConfigDict(env_file=PROJECT_ROOT / '.env', extra='ignore')

    @field_validator('data_dir', mode='after')
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @property
    def upload_dir(self) -> Path: return self.data_dir / 'uploads'

    def init(self): self.data_dir.mkdir(parents=True,exist_ok=True); self.upload_dir.mkdir(parents=True,exist_ok=True)
settings=Settings(); settings.init()
