"""Host-header allow-list and optional bearer-token auth for the API."""

import hmac
import ipaddress
from urllib.parse import parse_qs

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from osint.config import LOOPBACK_HOSTS

PROTECTED_PREFIXES = ("/api", "/ws", "/kml", "/metrics", "/docs", "/redoc", "/openapi.json")
# Google Earth NetworkLinks and browser WebSockets can't send headers
QUERY_TOKEN_PREFIXES = ("/kml", "/ws")


def is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _protected(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def request_token(scope: Scope) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == b"authorization":
            scheme, _, cred = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and cred.strip():
                return cred.strip()
    if _protected(scope.get("path", ""), QUERY_TOKEN_PREFIXES):
        qs = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        if qs.get("token"):
            return qs["token"][0]
    return None


class TokenAuthMiddleware:
    """Requires ``api_token`` on protected routes; a no-op when the token is empty."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (not self.token or scope["type"] not in ("http", "websocket")
                or not _protected(scope.get("path", ""), PROTECTED_PREFIXES)):
            await self.app(scope, receive, send)
            return
        given = request_token(scope)
        if given is not None and hmac.compare_digest(given.encode(), self.token.encode()):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        resp = PlainTextResponse("unauthorized", status_code=401,
                                 headers={"WWW-Authenticate": "Bearer"})
        await resp(scope, receive, send)


__all__ = ["LOOPBACK_HOSTS", "TokenAuthMiddleware", "is_loopback", "request_token"]
