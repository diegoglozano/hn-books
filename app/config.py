import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_path: Path = Path("data/hn_books.db")
    openlibrary_base_url: str = "https://openlibrary.org"
    hn_base_url: str = "https://hacker-news.firebaseio.com/v0"
    algolia_base_url: str = "https://hn.algolia.com/api/v1"
    hn_thread_ids: str = ""
    http_user_agent: str = "HNOpinionatedLibrary/0.1"
    http_timeout: float = 30
    hn_fetch_workers: int = 8
    metadata_interval: float = 1.1
    admin_token: str = ""
    frontend_path: Path = Path("frontend/dist")
    extraction_backend: Literal["luna", "heuristic"] = "luna"
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-6-luna"

    @property
    def thread_ids(self) -> list[int]:
        return [int(value.strip()) for value in self.hn_thread_ids.split(",") if value.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def library_config() -> dict:
    with (Path(__file__).parent / "data/library.toml").open("rb") as file:
        return tomllib.load(file)
