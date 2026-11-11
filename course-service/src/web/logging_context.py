"""Provide logging context operations."""

import logging

from src.web.middleware import request_id_context


class RequestIdFilter(logging.Filter):
    """Represent RequestIdFilter data."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get() or "-"
        return True
