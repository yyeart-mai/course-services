"""Web-application factory"""

from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.exceptions import RequestValidationError

from src.core.config import Settings
from src.web.errors import (
    ApiError,
    api_error_handler,
    validation_error_handler,
)
from src.web.middleware import RequestIdMiddleware


def create_app(
    settings: Settings,
    routers: Iterable[APIRouter],
    **params: Any,
) -> FastAPI:
    """Creates app with given parameters and routers"""
    app_params = {**settings.app.model_dump(), **params}
    app = FastAPI(**app_params)

    app.add_middleware(RequestIdMiddleware)

    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_error_handler,  # type: ignore[arg-type]
    )

    for router in routers:
        app.include_router(router)

    return app
