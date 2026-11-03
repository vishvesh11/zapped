"""Pydantic request/response models for the Buzz-wire backend API."""
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from config import settings


class ConfigResponse(BaseModel):
    shock_enabled: bool
    shock_duration: float
    current_media_file: str
    shock_duration_min: float = settings.shock_min_s
    shock_duration_max: float = settings.shock_max_s
    score_base: float = settings.score_base
    score_per_second: float = settings.score_per_second
    score_per_fault: float = settings.score_per_fault


class ConfigUpdate(BaseModel):
    shock_enabled: Optional[bool] = None
    shock_duration: Optional[float] = Field(
        None, ge=settings.shock_min_s, le=settings.shock_max_s
    )
    current_media_file: Optional[str] = Field(None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def at_least_one(self):
        if (self.shock_enabled is None and self.shock_duration is None
                and self.current_media_file is None):
            raise ValueError("provide at least one field to update")
        return self


class MediaListResponse(BaseModel):
    files: List[str]


class SessionStart(BaseModel):
    player_name: str = Field(min_length=1, max_length=24)

    @field_validator("player_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("player_name must not be blank")
        return v


class SessionResponse(BaseModel):
    active: bool
    session_id: Optional[str] = None
    player_name: Optional[str] = None
    phase: Optional[str] = None
    touch_count: int = 0
    shocks_fired: int = 0
    elapsed_seconds: float = 0.0
    live_score: int = 0


class RunStart(BaseModel):
    session_id: str = Field(max_length=64)
    device_id: Optional[str] = Field(None, max_length=64)


class TouchEvent(BaseModel):
    device_id: Optional[str] = Field(None, max_length=64)
    session_id: Optional[str] = Field(None, max_length=64)
    fault_count: Optional[int] = Field(None, ge=0, le=10000)
    uptime_ms: Optional[int] = Field(None, ge=0)


class TouchResponse(BaseModel):
    chime_queued: bool
    running: bool
    touch_count: int
    shock: bool
    shock_duration: float


class CompleteRequest(BaseModel):
    session_id: str = Field(max_length=64)
    touch_count: int = Field(ge=0, le=10000)
    completion_time_seconds: float = Field(gt=0, le=86400)


class LeaderboardEntry(BaseModel):
    rank: int
    player_name: str
    touch_count: int
    completion_time_seconds: float
    score: int
    session_id: Optional[str] = None


class PhotoItem(BaseModel):
    key: str
    url: str
    session_id: str
    timestamp: str
