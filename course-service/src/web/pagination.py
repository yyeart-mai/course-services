"""Provide pagination operations."""

from typing import Literal

from pydantic import BaseModel

SortOrder = Literal["created_at:asc", "created_at:desc"]


class Pagination(BaseModel):
    """Represent Pagination data."""

    offset: int
    limit: int
    total: int
