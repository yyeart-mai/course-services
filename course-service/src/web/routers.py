"""Define routes for gettings service information"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from src.web.lifespans import lifespan_service_router

logger = logging.getLogger(__name__)


class ServiceInfo(BaseModel):
    """Represent ServiceInfo data."""

    healthy: bool = True


service_router = APIRouter(
    prefix="/service",
    lifespan=lifespan_service_router,
    tags=["service"],
)


@service_router.get(
    "/",
    description="Service info route",
    response_model=ServiceInfo,
    status_code=200,
)
def info() -> ServiceInfo:
    """Returns service status"""
    return ServiceInfo()
