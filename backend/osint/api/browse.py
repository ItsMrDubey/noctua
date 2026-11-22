"""In-app browser route: reader view for pages that cannot be embedded."""

import httpx
from fastapi import FastAPI, HTTPException

from osint.browse import BlockedURL, Browser


def mount_browse(app: FastAPI, browser: Browser | None = None, alterlab_key: str = "") -> Browser:
    b = browser or Browser(alterlab_key=alterlab_key)

    async def guard(coro):
        try:
            return await coro
        except BlockedURL as exc:
            raise HTTPException(400, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"upstream: {type(exc).__name__}") from None

    @app.get("/api/browse/read")
    async def read(url: str) -> dict:
        return await guard(b.read(url))

    return b
