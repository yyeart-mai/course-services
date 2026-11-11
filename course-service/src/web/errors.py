from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):

    code: str
    message: str
    status: int
    path: str
    request_id: str


class ErrorEnvelope(BaseModel):

    error: ErrorDetail


def documented_errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    errors: dict[int | str, dict[str, Any]] = {
        status: {"model": ErrorEnvelope} for status in statuses
    }
    if 401 in errors:
        errors[401]["headers"] = {"WWW-Authenticate": {"schema": {"type": "string"}}}
    return errors


class ApiError(Exception):

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def error_response(
    request: Request, status: int, code: str, message: str
) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return JSONResponse(
        status_code=status,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "status": status,
                "path": request.url.path,
                "request_id": request.state.request_id,
            }
        },
    )


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(request, exc.status, exc.code, exc.message)


async def validation_error_handler(
    request: Request, _exc: RequestValidationError
) -> JSONResponse:
    return error_response(
        request, 422, "VALIDATION_ERROR", "Invalid request parameters"
    )
