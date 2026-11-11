from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from src.web.pagination import Pagination
from src.web.patch_validation import validate_patch_payload

AssignmentText = Annotated[str, Field(min_length=1)]
AttemptsLimit = Annotated[int, Field(strict=True, ge=3, le=10)]


class AssignmentCreate(BaseModel):

    model_config = ConfigDict(extra="forbid")

    title: AssignmentText
    description: AssignmentText
    course_id: UUID
    date_start: date
    date_end: date
    attempts_limit: AttemptsLimit = 3
    materials_urls: list[AnyHttpUrl] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_dates(self) -> "AssignmentCreate":
        if self.date_start > self.date_end:
            raise ValueError("date_start must not exceed date_end")
        return self


class AssignmentPatch(BaseModel):

    model_config = ConfigDict(extra="forbid")

    title: AssignmentText | None = None
    description: AssignmentText | None = None
    date_start: date | None = None
    date_end: date | None = None
    attempts_limit: AttemptsLimit | None = None
    materials_urls: list[AnyHttpUrl] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_empty_or_null(cls, value: Any) -> Any:
        return validate_patch_payload(value)


class AssignmentRead(BaseModel):

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: AssignmentText
    description: AssignmentText
    course_id: UUID
    date_start: date
    date_end: date
    attempts_limit: AttemptsLimit
    materials_urls: list[AnyHttpUrl]
    created_at: datetime
    updated_at: datetime


class AssignmentPage(BaseModel):

    data: list[AssignmentRead] = Field(max_length=100)
    pagination: Pagination
