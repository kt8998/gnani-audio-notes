"""
All configuration comes from environment variables (or backend/.env locally).
Nothing secret is hard-coded, and none of these values are ever sent to the browser.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    database_url: str = ""

    gnani_api_key: str = ""
    gnani_stt_url: str = "https://api.vachana.ai/stt/v3"

    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "audio-notes"
    s3_region: str = "us-east-1"  # Supabase: set to the project's region (part of the request signature)

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"

    cors_origins: str = "http://localhost:3000"

    # Supabase Storage free plan: max 50 MB per object. Reject bigger files up front.
    max_upload_mb: int = 50
    # Gnani rejects audio longer than 30s (verified against the live API), so stay well under it.
    chunk_seconds: int = 25
    transcribe_concurrency: int = 3

    use_os_truststore: bool = False

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, url: str) -> str:
        # Railway/Neon hand out "postgresql://..." or "postgres://...".
        # SQLAlchemy needs the driver named explicitly to use psycopg 3.
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def setup_tls() -> None:
    """On dev machines with HTTPS-intercepting antivirus, trust the OS certificate store."""
    if get_settings().use_os_truststore:
        import truststore

        truststore.inject_into_ssl()
