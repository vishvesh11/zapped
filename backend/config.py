"""Application settings for the Buzz-wire game backend.

All configuration is centralised here using pydantic-settings so values can be
supplied through environment variables (or a local ``.env`` file) and validated
once at import time.
"""
from pathlib import Path
from typing import Set

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Paths ------------------------------------------------------------ #
    media_dir: Path = Field(default=BASE_DIR / "media", alias="MEDIA_DIR")
    templates_dir: Path = Field(default=BASE_DIR / "templates", alias="TEMPLATES_DIR")
    db_path: str = Field(default=str(BASE_DIR / "leaderboard.db"), alias="DB_PATH")

    # ---- Auth ------------------------------------------------------------- #
    api_key: str = Field(default="", alias="GAME_API_KEY")

    # ---- Media ------------------------------------------------------------ #
    default_media: str = "chime.wav"
    allowed_ext: Set[str] = {".wav", ".ogg", ".mp3"}

    # ---- Headscale camera + Garage S3 ------------------------------------- #
    phone_snapshot_url: str = Field(
        default="http://100.64.0.8:8080/shot.jpg", alias="PHONE_SNAPSHOT_URL"
    )
    s3_endpoint: str = Field(default="https://s3.vishvesh.me", alias="S3_ENDPOINT")
    s3_region: str = Field(default="garage", alias="S3_REGION")
    s3_bucket: str = Field(default="buzzwire-zaps", alias="S3_BUCKET")
    s3_access_key: str = Field(default="", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="", alias="S3_SECRET_KEY")

    # ---- Score formula ---------------------------------------------------- #
    score_base: float = Field(default=1000.0, alias="SCORE_BASE")
    score_per_second: float = Field(default=5.0, alias="SCORE_PER_SECOND")
    score_per_fault: float = Field(default=50.0, alias="SCORE_PER_FAULT")

    # ---- Server ----------------------------------------------------------- #
    port: int = Field(default=8000, alias="PORT")

    @property
    def media_dir_resolved(self) -> Path:
        return self.media_dir.resolve()

    @property
    def templates_dir_resolved(self) -> Path:
        return self.templates_dir.resolve()


settings = Settings()
######