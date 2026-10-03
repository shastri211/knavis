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
    nvidia_base_url:str='https://integrate.api.nvidia.com/v1'; nvidia_ocr_base_url:str='https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2'; groq_base_url:str='https://api.groq.com/openai/v1'; openrouter_base_url:str='https://openrouter.ai/api/v1'
    qdrant_url:str=''; qdrant_api_key:str=''; qdrant_collection:str=''; openrouter_model:str=''
    embedding_model:str='nvidia/nemotron-3-embed-1b'; embedding_dimensions:int|None=None
    default_provider:str='groq'; default_model:str='openai/gpt-oss-20b'; max_upload_mb:int=50; max_message_chars:int=20000; max_agent_retrieval_calls:int=2; top_k_rerank:int=12
    # Evidence gate. A chunk is usable when its dense cosine similarity reaches
    # evidence_min_dense OR it covers at least evidence_min_coverage of the question's
    # content terms. At most evidence_max_items chunks are sent to the LLM.
    evidence_min_dense:float=0.35; evidence_min_coverage:float=0.5; evidence_max_items:int=8
    # Upload safety. A small file can still describe a huge one, so archives, PDFs and images are checked first.
    max_archive_entries:int=5000
    # Macros, embedded programs and PDF JavaScript are refused by default. KNAVIS only reads documents' text and never runs
    # anything in them, but a file with active content is a poor thing to keep and pass on.
    allow_active_content:bool=False
    max_unpacked_mb:int=500
    max_pdf_pages:int=1000
    max_image_megapixels:int=100
    max_documents_per_session:int=50
    max_user_storage_mb:int=1000
    # Accounts. With AUTH_ENABLED=false the app is single-user and every chat is visible (local use only).
    auth_enabled:bool=True
    allow_registration:bool=True
    token_ttl_days:int=30
    # Requests per minute per signed-in user (per client address for sign-in itself). 0 turns a limit off.
    rate_limit_chat_per_minute:int=30
    rate_limit_upload_per_minute:int=12
    rate_limit_auth_per_minute:int=10
    # Only behind a reverse proxy you control: take the client address from X-Forwarded-For.
    trust_proxy_headers:bool=False
    # Empty: SQLite in the data directory. Or e.g. postgresql://user:password@host:5432/knavis (see DEPLOYMENT.md).
    database_url:str=''
    # Optional virus scanning of uploads with ClamAV (clamd). CLAMAV_REQUIRED=true refuses uploads while it is down.
    clamav_host:str=''; clamav_port:int=3310; clamav_timeout:float=30.0; clamav_required:bool=False
    # Password reset by e-mail appears only when SMTP is configured. PUBLIC_URL is where people open the app (for the link).
    smtp_host:str=''; smtp_port:int=587; smtp_user:str=''; smtp_password:str=''; smtp_from:str=''; smtp_starttls:bool=True
    public_url:str=''
    reset_token_minutes:int=60
    # New accounts must confirm their e-mail address before they can sign in. Needs SMTP (otherwise it has no effect).
    require_email_verification:bool=False
    verification_token_hours:int=48
    # Several processes may share one database. A job is held with a lease the owner keeps renewing; if the owner dies
    # the lease runs out and another process takes the job over. INGESTION_INLINE=false leaves all processing to
    # dedicated workers (python -m app.worker); the default runs jobs in the process that received the upload too.
    job_lease_seconds:int=30
    job_sweep_seconds:float=15.0
    job_poll_seconds:float=2.0
    ingestion_inline:bool=True
    # Shared rate limits across processes: redis://host:6379/0. Empty: counted in each process separately.
    redis_url:str=''
    # How many documents are extracted and indexed at once; the rest wait their turn.
    ingestion_concurrency:int=2
    # Spreadsheet analytics: questions that need computing (totals, counts, the highest group) are answered with
    # a read-only SQL query over the sheet, written by one model call and strictly validated before it runs.
    analytics_enabled:bool=True
    analytics_timeout_seconds:float=5.0     # a query running longer is cancelled
    analytics_max_rows:int=200              # rows a query may return
    # Hosted specialists (OCR, figure description, speech-to-text). Every *_provider setting is
    # "auto" (first configured provider in the documented order), a provider name, or "none".
    mistral_api_key:str=''; gemini_api_key:str=''
    ocr_provider:str='auto'              # mistral | nvidia
    vision_provider:str='auto'           # groq | gemini
    vision_model:str='qwen/qwen3.8-27b'  # Groq vision model id (provider catalogs change; verify with check_config)
    gemini_vision_model:str='gemini-2.5-flash-lite'
    asr_provider:str='auto'              # groq | assemblyai
    asr_model:str='whisper-large-v3-turbo'
    # Work above this many hosted calls for one document waits for confirmation instead of spending quota.
    confirm_above_calls:int=25
    vision_max_figures_per_doc:int=8
    # Gemini's free tier uses submitted content to improve Google's products. It stays off unless the
    # owner of the data opts in here.
    allow_free_tier_data_use:bool=False
    # Override the built-in conservative quota defaults with your account's real limits, e.g.
    # QUOTA_OVERRIDES={"mistral_ocr":{"pages":{"minute":60,"day":2000}}}
    quota_overrides:dict[str,dict[str,dict[str,int]]]={}
    model_config=SettingsConfigDict(env_file=PROJECT_ROOT / '.env', extra='ignore')

    @field_validator('data_dir', mode='after')
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @property
    def upload_dir(self) -> Path: return self.data_dir / 'uploads'

    def init(self): self.data_dir.mkdir(parents=True,exist_ok=True); self.upload_dir.mkdir(parents=True,exist_ok=True)
settings=Settings(); settings.init()
