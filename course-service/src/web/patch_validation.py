from typing import Any


def validate_patch_payload(value: Any) -> Any:
    if (
        not isinstance(value, dict)
        or not value
        or any(item is None for item in value.values())
    ):
        raise ValueError("PATCH must contain non-null fields")
    return value
