from typing import Literal

from pydantic import BaseModel

SortOrder = Literal["created_at:asc", "created_at:desc"]


class Pagination(BaseModel):

    offset: int
    limit: int
    total: int
