from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Mark = Annotated[float, Field(ge=0, le=10, multiple_of=0.01)]
ReviewStatus = Literal["PASSED", "FAILED"]


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback: str
    mark: Mark
    status: ReviewStatus


class ReviewPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback: str | None = None
    mark: Mark | None = None
    status: ReviewStatus | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_empty_or_null(cls, value: Any) -> Any:
        if (
            not isinstance(value, dict)
            or not value
            or any(item is None for item in value.values())
        ):
            raise ValueError("PATCH must contain non-null fields")

        return value


class ReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: Literal["auto", "manual"]
    feedback: str
    mark: Mark
    status: ReviewStatus
    created_at: datetime
    updated_at: datetime
