"""Provide client operations."""

from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, HttpUrl


class AutoReviewResult(BaseModel):
    """Represent AutoReviewResult data."""

    feedback: str
    mark: float = Field(ge=0, le=10, multiple_of=0.01)
    status: Literal["PASSED", "FAILED"]


class ArtifactResult(BaseModel):
    """Represent ArtifactResult data."""

    name: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    download_url: HttpUrl


class CheckResult(BaseModel):
    """Represent CheckResult data."""

    submission_id: UUID
    status: Literal["COMPLETED"]
    review: AutoReviewResult | None = None
    artifacts: list[ArtifactResult] = Field(default_factory=list)


async def call_checker(
    client: httpx.AsyncClient,
    checker_url: str,
    submission_id: UUID,
    repository_url: str,
) -> CheckResult:
    """Call the external checker and validate its response."""
    response = await client.post(
        checker_url,
        json={
            "submission_id": str(submission_id),
            "repository_url": repository_url,
        },
    )

    response.raise_for_status()

    return CheckResult.model_validate(response.json())
