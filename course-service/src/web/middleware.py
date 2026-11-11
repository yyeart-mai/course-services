import logging
from contextvars import ContextVar
from uuid import UUID, uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


class RequestIdMiddleware:

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_id = dict(scope["headers"]).get(b"x-request-id")
        try:
            request_id = str(UUID(raw_id.decode("ascii"))) if raw_id else str(uuid4())
        except UnicodeDecodeError, ValueError:
            request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_context.set(request_id)

        logger.info("HTTP request started")

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("ascii"))
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_context.reset(token)
