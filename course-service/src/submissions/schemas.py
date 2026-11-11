from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, AnyUrl, BaseModel, ConfigDict, Field


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: UUID
    repository_url: AnyUrl


class SubmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    assignment_id: UUID
    student_id: UUID
    submitted_at: datetime
    review_id: UUID | None
    repository_url: AnyUrl
    status: str
    artifacts_url: AnyHttpUrl | None
    created_at: datetime
    updated_at: datetime


class ArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    download_url: AnyHttpUrl
