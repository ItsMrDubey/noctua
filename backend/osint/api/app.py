"""HTTP API: health, metrics, sources and live summary."""

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from osint.api import layers
from osint.api.licences import mount_licences
from osint.api.m1 import mount_m1
from osint.api.security import TokenAuthMiddleware
from osint.runtime import Runtime


def create_app(runtime: Runtime) -> FastAPI:
    """``runtime`` is a Runtime (`osint run`) or an ApiRuntime (`osint api`, which also
    reports the worker process in /health)."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await runtime.start()
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="OSINT", version="0.1.0", lifespan=lifespan)
    settings = runtime.settings
    app.state.api_token = settings.api_token
    # last added runs first: reject foreign Host headers (DNS rebinding) before auth
    app.add_middleware(TokenAuthMiddleware, token=settings.api_token)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

    @app.get("/health")
    def health() -> dict:
        sources = {}
        for name, h in runtime.scheduler.health.items():
            d = asdict(h)
            d["last_success"] = h.last_success.isoformat() if h.last_success else None
            sources[name] = d
        ingestor = {
            "alive": runtime.ingestor_alive,
            "errors": runtime.ingestor.errors,
            "queue_depth": runtime.ingestor.queue.qsize(),
            "consecutive_flush_failures": getattr(
                runtime.ingestor, "consecutive_flush_failures", 0
            ),
        }
        out: dict = {"sources": sources, "ingestor": ingestor}
        worker_info = getattr(runtime, "worker_info", None)
        if worker_info is not None:  # split mode: the pipeline runs in `osint worker`
            out["worker"] = worker = worker_info()
            if not worker.pop("alive"):
                return {"status": "worker_down", **out}
        if runtime.booting:
            return {"status": "starting", **out}
        if runtime.boot_error:
            return {"status": "failed", "boot_error": runtime.boot_error, **out}
        all_down = bool(sources) and all(s["status"] == "down" for s in sources.values())
        status = "ok" if ingestor["alive"] and not all_down else "degraded"
        return {"status": status, **out}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return runtime.metrics.render_prometheus()

    @app.get("/api/sources")
    def sources() -> list[dict]:
        return runtime.store.list_sources()

    @app.get("/api/live/summary")
    def live_summary() -> dict:
        return {"tracks": layers.live_counts(runtime),
                "quarantined": runtime.store.quarantine_count()}

    mount_licences(app)
    mount_m1(app, runtime)
    return app
