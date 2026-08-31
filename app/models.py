from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    POSTPROCESSING = "postprocessing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATUSES = {
    OperationStatus.COMPLETED.value,
    OperationStatus.FAILED.value,
    OperationStatus.CANCELLED.value,
    OperationStatus.EXPIRED.value,
}


class AnalysisCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    url: str = Field(min_length=8, max_length=16384)
    playlist: bool = False


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    analysis_id: UUID
    choice_id: UUID
    subtitle_languages: list[str] = Field(default_factory=list, max_length=5)
    include_auto_subtitles: bool = False
    embed_metadata: bool = True

    @field_validator("subtitle_languages")
    @classmethod
    def validate_subtitle_languages(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value or len(value) > 32:
                raise ValueError("subtitle language codes must be 1-32 characters")
            if not all(char.isalnum() or char in {"-", "_", "."} for char in value):
                raise ValueError("invalid subtitle language code")
            if value not in cleaned:
                cleaned.append(value)
        return cleaned


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=1024)


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class AnalysisPublic(BaseModel):
    id: str
    status: OperationStatus
    playlist: bool
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    result: dict[str, Any] | None = None
    error: ErrorBody | None = None


class ArtifactPublic(BaseModel):
    id: str
    filename: str
    size: int
    media_type: str
    sha256: str
    primary: bool
    created_at: datetime
    expires_at: datetime
    download_url: str


class JobPublic(BaseModel):
    id: str
    analysis_id: str
    status: OperationStatus
    phase: str
    progress: float = Field(ge=0, le=100)
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None
    eta: int | None = None
    playlist_index: int | None = None
    playlist_count: int | None = None
    title: str | None = None
    platform: str | None = None
    choice: dict[str, Any] | None = None
    artifacts: list[ArtifactPublic] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime
    cancel_requested: bool = False
    error: ErrorBody | None = None


class PaginatedJobs(BaseModel):
    items: list[JobPublic]
    total: int
    limit: int
    offset: int


class SessionPublic(BaseModel):
    auth_required: bool
    authenticated: bool


class HealthPublic(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    checks: dict[str, Any]
