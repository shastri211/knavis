from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
class Settings(BaseSettings):
    host:str='127.0.0.1'; port:int=8000; frontend_origin:str='http://127.0.0.1:5173'
    data_dir:Path=Path('data'); nvidia_api_key:str=''; groq_api_key:str=''; openrouter_api_key:str=''; assemblyai_api_key:str=''
    nvidia_base_url:str='https://integrate.api.nvidia.com/v1'; nvidia_ocr_base_url:str='https://ai.api.nvidia.com/v1/ocr'; groq_base_url:str='https://api.groq.com/openai/v1'; openrouter_base_url:str='https://openrouter.ai/api/v1'
    qdrant_url:str=''; qdrant_api_key:str=''; qdrant_collection:str=''; openrouter_model:str=''
    embedding_model:str='nvidia/llama-nemotron-embed-1b-v2'; embedding_dimensions:int|None=None
    default_provider:str='nvidia'; default_model:str='meta/llama-3.1-8b-instruct'; max_upload_mb:int=50; max_message_chars:int=20000; max_agent_retrieval_calls:int=2; top_k_rerank:int=12
    model_config=SettingsConfigDict(env_file=PROJECT_ROOT / '.env', extra='ignore')
    def init(self): self.data_dir.mkdir(parents=True,exist_ok=True); (self.data_dir/'uploads').mkdir(exist_ok=True)
settings=Settings(); settings.init()
