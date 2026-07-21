"""Application settings (pydantic-settings).

All paths default to an in-container ``/data`` layout that is bind-mounted (cookies.json +
downloads/ + archive/ persist in a named volume). Cookies are NEVER read from the environment;
they are managed at runtime via the Settings UI and stored only in ``cookies_path``.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # allow list[str] to be read from a comma-separated env var
        env_parse_none_str="",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    data_dir: Path = Path("/data")
    downloads_dir: Path = Path("/data/downloads")
    cookies_path: Path = Path("/data/cookies.json")

    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    max_concurrent_jobs: int = 2

    # Interpreter used to spawn the per-job gallery-dl worker subprocess.
    # Empty string -> resolved to sys.executable at runtime.
    worker_python: str = ""


def get_settings() -> Settings:
    """Factory (no caching) — callers create app.state singletons explicitly."""
    return Settings()
