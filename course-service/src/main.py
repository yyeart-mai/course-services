"""Configure logging and expose the application"""

import logging
import logging.config

import yaml
from fastapi import FastAPI

from src.assignments.router import assignments_router
from src.core.config import get_settings
from src.courses.router import courses_router
from src.submissions.router import submissions_router
from src.web.app import create_app
from src.web.lifespans import lifespan_app
from src.web.routers import service_router

logger = logging.getLogger(__name__)


def main() -> FastAPI:
    """Build and configure the FastAPI application."""
    with open("log-config.yaml", "r", encoding="UTF-8") as f:
        log_config = yaml.safe_load(f)
        logging.config.dictConfig(log_config)

    settings = get_settings()

    routers = (
        service_router,
        courses_router,
        assignments_router,
        submissions_router,
    )

    logger.debug("Created app version=%s", settings.app.version)
    return create_app(settings, routers, lifespan=lifespan_app)


app = main()
