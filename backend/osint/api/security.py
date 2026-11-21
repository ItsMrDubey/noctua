"""Host-header allow-list and optional bearer-token auth for the API."""

import hashlib
import hmac
import ipaddress
import re
import time
from urllib.parse import parse_qs

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from osint.config import LOOPBACK_HOSTS

PROTECTED_PREFIXES = ("/api", "/ws", "/kml", "/metrics", "/docs", "/redoc", "/openapi.json")
# Google Earth NetworkLinks and browser WebSockets can't send headers
QUERY_TOKEN_PREFIXES = ("/kml", "/ws")


# <img> / Cesium billboards cannot send an Authorization header: these GET paths also accept a
# short-lived HMAC signature (?sig=...&exp=...) minted by an authenticated API response.
SIGNED_PATHS = (re.compile(r"^/api/webcams/[A-Za-z0-9_.:\-]{1,100}/snapshot$"),
                re.compile(r"^/api/imagery/[A-Za-z0-9_.\-]{1,100}/[A-Za-z0-9_.\-]{1,40}$"))
SIGN_TTL_S = 300


def signable(path: str) -> bool:
    return any(p.match(path) for p in SIGNED_PATHS)


def _mac(token: str, path: str, exp: int) -> str:
    return hmac.new(token.encode(), f"{path}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def sign_path(token: str, path: str, ttl: int = SIGN_TTL_S, now: float | None = None) -> str:
    """``path`` with ``?sig=&exp=`` appended (the path itself, unchanged, when no token is set)."""
    if not token or not signable(path.split("?", 1)[0]):
        return path
    exp = int((time.time() if now is None else now) + ttl)
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}sig={_mac(token, path.split('?', 1)[0], exp)}&exp={exp}"


def valid_signature(token: str, path: str, query: str, now: float | None = None) -> bool:
    qs = parse_qs(query)
    try:
        sig, exp = qs["sig"][0], int(qs["exp"][0])
    except (KeyError, IndexError, ValueError):
        return False
    if exp < (time.time() if now is None else now) or not signable(path):
        return False
    return hmac.compare_digest(sig.encode(), _mac(token, path, exp).encode())


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
        if (scope["type"] == "http" and scope.get("method") in ("GET", "HEAD")
                and valid_signature(self.token, scope.get("path", ""),
                                    (scope.get("query_string") or b"").decode("latin-1"))):
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


__all__ = ["LOOPBACK_HOSTS", "TokenAuthMiddleware", "is_loopback", "request_token", "sign_path", "signable"]
