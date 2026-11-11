"""Validate partial update payloads shared by resource schemas."""

from typing import Any


def validate_patch_payload(value: Any) -> Any:
    """Reject empty patches and fields explicitly set to null."""
    if (
        not isinstance(value, dict)
        or not value
        or any(item is None for item in value.values())
    ):
        raise ValueError("PATCH must contain non-null fields")
    return value
