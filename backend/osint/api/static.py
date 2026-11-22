"""Static frontend serving: built bundle when present, precompressed files, cache policy.

- ``/`` serves ``frontend/dist/index.html`` (the production build from
  ``python -m osint.frontend_build``) when it exists, else the source
  ``frontend/index.html``. ``OSINT_FRONTEND=src`` forces the source tree.
- ``/static/...`` sends a ``<file>.gz`` sibling with ``Content-Encoding: gzip`` when the
  client accepts gzip (Cesium.js: 6.1 MB -> 1.8 MB) and sets Cache-Control by path:
  content-hashed bundle assets are immutable, vendored Cesium/models are cached for a
  day, everything else revalidates (ETag) so source edits show up on reload.
- JSON API responses over 1 KB are gzip-compressed on the fly.
"""

from __future__ import annotations

import mimetypes
import os
import stat
from pathlib import Path

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# Windows takes MIME types from the registry, which can map .js to text/plain; module
# scripts are rejected unless served as JavaScript
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("model/gltf-binary", ".glb")

IMMUTABLE ="public, max-age=31536000, immutable"
DAY = "public, max-age=86400"
REVALIDATE = "no-cache"


def cache_control(path: str) -> str:
    p = path.replace("\\", "/").lstrip("/")
    if p.startswith("dist/assets/"):
        return IMMUTABLE
    if p.startswith(("vendor/", "models/")):
        return DAY
    return REVALIDATE


def index_path(frontend: Path) -> Path:
    built = frontend / "dist" / "index.html"
    if os.environ.get("OSINT_FRONTEND", "").lower() != "src" and built.is_file():
        return built
    return frontend / "index.html"


def index_response(frontend: Path) -> Response | None:
    f = index_path(frontend)
    if not f.is_file():
        return None
    return FileResponse(f, headers={"Cache-Control": REVALIDATE})


def _accepts_gzip(scope: Scope) -> bool:
    return "gzip" in Headers(scope=scope).get("accept-encoding", "").lower()


class FrontendFiles(StaticFiles):
    def _fresh_gz(self, path: str) -> tuple[str, os.stat_result | None]:
        """The ``.gz`` sibling of ``path`` if it is a regular file at least as new as the
        original (a stale one would serve outdated content)."""
        try:
            full, gz = self.lookup_path(path + ".gz")
            if gz is None or not stat.S_ISREG(gz.st_mode):
                return "", None
            _, orig = self.lookup_path(path)
        except (OSError, ValueError):
            return "", None
        if orig is None or orig.st_mtime > gz.st_mtime:
            return "", None
        return full, gz

    async def get_response(self, path: str, scope: Scope) -> Response:
        if scope["method"] in ("GET", "HEAD") and _accepts_gzip(scope) \
                and not path.endswith(".gz"):
            full, st = self._fresh_gz(path)
            if st is not None:
                media = mimetypes.guess_type(path)[0] or "application/octet-stream"
                if media.startswith("text/") or media in ("application/javascript",):
                    media += "; charset=utf-8"
                resp = FileResponse(full, stat_result=st, media_type=media, headers={
                    "Content-Encoding": "gzip", "Vary": "Accept-Encoding",
                    "Cache-Control": cache_control(path)})
                if self.is_not_modified(resp.headers, Headers(scope=scope)):
                    return Response(status_code=304, headers={
                        k: v for k, v in resp.headers.items()
                        if k in ("etag", "cache-control", "vary", "last-modified")})
                return resp
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            resp.headers["Cache-Control"] = cache_control(path)
        return resp


def mount_static(app: FastAPI, frontend: Path) -> None:
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.mount("/static", FrontendFiles(directory=frontend, check_dir=False), name="static")
