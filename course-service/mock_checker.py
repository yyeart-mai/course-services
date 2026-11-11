"""Serve deterministic checker responses for local development."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from pydantic import AnyUrl, BaseModel

from src.checker.client import AutoReviewResult, CheckResult

app = FastAPI(title="Checker mock")


class CheckRequest(BaseModel):
    """Describe the request sent by the checker worker."""

    submission_id: UUID
    repository_url: AnyUrl


@app.post("/check")
async def check(
    payload: CheckRequest,
    delay_seconds: Annotated[float, Query(ge=0)] = 0,
    fail: bool = False,
) -> CheckResult:
    """Return a delayed success or a requested HTTP failure."""
    await asyncio.sleep(delay_seconds)

    if fail:
        raise HTTPException(status_code=503, detail="Mock checker failure")

    return CheckResult(
        submission_id=payload.submission_id,
        status="COMPLETED",
        review=AutoReviewResult(
            feedback="Автопроверка завершена",
            mark=9.5,
            status="PASSED",
        ),
        artifacts=[],
    )
