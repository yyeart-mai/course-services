from uuid import UUID

from fastapi import Request

from src.web.errors import ApiError

IDEMPOTENCY_HEADER = {
    "parameters": [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "format": "uuid"},
        }
    ]
}


def get_idempotency_key(request: Request) -> UUID:
    raw = request.headers.get("Idempotency-Key")

    if raw is None:
        raise ApiError(422, "IDEMPOTENCY_KEY_MISSING", "Idempotency-Key is required")

    try:
        return UUID(raw)

    except ValueError as e:
        raise ApiError(422, "VALIDATION_ERROR", "Idempotency-Key must be a UUID") from e
