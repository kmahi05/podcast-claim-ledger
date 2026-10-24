from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]

JWT_ISSUER = "podcast-ledger"
JWT_AUDIENCE = "podcast-ledger-api"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    APP_DATABASE_URL: str = "postgresql+asyncpg://pcl_app:pcl_app@localhost:5432/podcast_ledger"
    WORKER_DATABASE_URL: str = (
        "postgresql+asyncpg://pcl_worker:pcl_worker@localhost:5432/podcast_ledger"
    )

    JWT_SECRET: str
    ACCESS_TOKEN_TTL_SECONDS: int = 900
    REFRESH_TOKEN_TTL_SECONDS: int = 1_209_600
    COOKIE_SECURE: bool = False
    WEB_ORIGIN: str = "http://localhost:3002"

    GOOGLE_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_MODEL_STRONG: str = "gemini-2.5-pro"
    GEMINI_EMBEDDING_MODEL: str = "models/gemini-embedding-001"
    LLM_CONCURRENCY: int = 4

    CHROMA_PATH: str = "./data/chroma"
    STORAGE_DIR: str = "./data/storage"
    FILE_URL_SECRET: str

    WHISPER_MODEL: str = "small.en"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    HF_TOKEN: str = ""
    AUDIO_RETENTION_DAYS: int = 0

    @field_validator("JWT_SECRET", "FILE_URL_SECRET")
    @classmethod
    def _long_secret(cls, v: str) -> str:
        if len(v.encode()) < 32:
            raise ValueError("must be at least 32 bytes")
        return v

    @field_validator("WHISPER_MODEL")
    @classmethod
    def _english_only(cls, v: str) -> str:
        # podcasts are English only; large-v3-turbo is allowed because language="en" is forced
        if not (v.endswith(".en") or v == "large-v3-turbo"):
            raise ValueError("use an English-only model (small.en, medium.en) or large-v3-turbo")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
