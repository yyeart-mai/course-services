"""Provide schemas operations."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.web.pagination import Pagination
from src.web.patch_validation import validate_patch_payload

CourseStatus = Literal["DRAFT", "ACTIVE", "CLOSED"]
CourseTitle = Annotated[str, StringConstraints(min_length=15, max_length=255)]
CourseDescription = Annotated[str, StringConstraints(min_length=32, max_length=4096)]


class CourseCreate(BaseModel):
    """Represent CourseCreate data."""

    model_config = ConfigDict(extra="forbid")

    title: CourseTitle
    description: CourseDescription
    teacher_id: UUID
    status: CourseStatus = "DRAFT"


class CoursePatch(BaseModel):
    """Represent CoursePatch data."""

    model_config = ConfigDict(extra="forbid")

    title: CourseTitle | None = None
    description: CourseDescription | None = None
    teacher_id: UUID | None = None
    status: CourseStatus | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_empty_or_null(cls, value: Any) -> Any:
        """Reject an empty patch or null field values."""
        return validate_patch_payload(value)


class CourseRead(BaseModel):
    """Represent CourseRead data."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: CourseTitle
    description: CourseDescription
    teacher_id: UUID
    status: CourseStatus
    created_at: datetime
    updated_at: datetime


class CoursePage(BaseModel):
    """Represent CoursePage data."""

    data: list[CourseRead] = Field(max_length=100)
    pagination: Pagination
