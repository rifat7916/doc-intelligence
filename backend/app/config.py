"""Central configuration. Every value can be overridden with an environment variable."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- infrastructure ---
    database_url: str = "sqlite:///./data/app.db"
    redis_url: str = "redis://localhost:6379/0"
    celery_always_eager: bool = False  # True = run jobs inline (tests / no-Redis dev mode)
    upload_dir: str = "./data/uploads"
    max_upload_mb: int = 200

    # --- security ---
    api_key: str = ""  # empty = auth disabled (local dev only)
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    rate_limit_per_minute: int = 0  # 0 = off; needs Redis

    # --- LLM / embeddings (Gemini) ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_fallback_models: str = "gemini-3.1-flash-lite,gemini-2.5-flash"
    gemini_thinking_level: str = "low"  # "" = do not send a thinking config
    gemini_embed_model: str = "gemini-embedding-001"
    embed_dim: int = 768  # changing this needs a DB migration
    extractor: str = "auto"  # auto | llm | rules
    embedder: str = "auto"  # auto | gemini | hash
    llm_concurrency: int = 4
    llm_max_retries: int = 4
    llm_timeout_s: int = 90
    llm_max_chars: int = 14000

    # --- processing ---
    ocr_lang: str = "eng"
    ocr_threads: int = 2
    ocr_dpi: int = 200
    ocr_psm: int = 4
    ocr_page_timeout_s: int = 60
    min_digital_chars: int = 40
    max_pages: int = 3000
    date_dayfirst: bool = True
    stale_job_seconds: int = 900  # a "processing" job untouched for this long is considered dead
    recover_interval_seconds: int = 300  # API checks for stuck jobs this often (0 = off)
    max_text_chunks_per_doc: int = 200

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def gemini_models(self) -> list[str]:
        extra = [m.strip() for m in self.gemini_fallback_models.split(",") if m.strip()]
        return [self.gemini_model, *[m for m in extra if m != self.gemini_model]]


@lru_cache
def get_settings() -> Settings:
    return Settings()
