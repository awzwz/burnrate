"""Application factory shared by every service: context, errors, health, metrics, lifecycle."""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from burnrate.common import bus, clients, db
from burnrate.common.config import settings
from burnrate.common.context import Ctx, reset_ctx, set_ctx
from burnrate.common.errors import DomainError, Forbidden
from burnrate.services import SERVICES, load

log = logging.getLogger("burnrate")

REQUESTS = Counter("br_http_requests_total", "HTTP requests", ["service", "method", "route", "status"])
LATENCY = Histogram("br_http_request_seconds", "HTTP request latency", ["service", "route"],
                    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))


async def require_internal(request: Request) -> None:
    """Internal routers answer only to callers that hold the internal token (other services)."""
    if not request.scope.get("br_internal"):
        raise Forbidden("internal endpoint", code="internal_only")


internal = [Depends(require_internal)]


class ContextMiddleware:
    """Pure ASGI: sets the request context from trusted headers, records metrics."""

    def __init__(self, app, service: str):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        trusted = hmac.compare_digest(headers.get("x-internal-token", ""), settings().internal_token)
        scope["br_internal"] = trusted
        ctx = Ctx.from_headers(headers) if trusted else Ctx(request_id=headers.get("x-request-id") or uuid.uuid4().hex)
        token = set_ctx(ctx)
        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", ctx.request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_ctx(token)
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            REQUESTS.labels(self.service, scope["method"], route, str(status["code"])).inc()
            LATENCY.labels(self.service, route).observe(time.perf_counter() - started)


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    err: dict = {"code": code, "message": message}
    if details:
        err["details"] = details
    return JSONResponse({"error": err}, status_code=status)


def make_app(names: list[str] | None = None, *, service_label: str | None = None) -> FastAPI:
    """Build an app that hosts the given services (all of them in local mode)."""
    st = settings()
    names = names or list(SERVICES)
    label = service_label or (names[0] if len(names) == 1 else "all")
    mods = [load(n) for n in names]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        for svc in db.known_services():
            for attempt in range(5):  # replicas may race on CREATE TABLE
                try:
                    await db.create_all(svc)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 4:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
        if st.is_local:
            clients.set_local_app(app)
            if not st.is_test:
                tasks.append(asyncio.create_task(bus.local_relay_loop()))
        elif st.kafka_bootstrap:
            for svc in db.known_services():
                tasks.append(asyncio.create_task(bus.kafka_relay_loop(svc)))
        for m in mods:
            if hasattr(m, "startup"):
                for t in (await m.startup(app)) or []:
                    tasks.append(t)
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await clients.close_all()
            await db.dispose_all()

    app = FastAPI(title=f"Burn Rate · {label}", version="0.1.0", lifespan=lifespan,
                  docs_url="/docs" if st.env != "prod" else None, redoc_url=None)
    app.add_middleware(ContextMiddleware, service=label)

    @app.exception_handler(DomainError)
    async def _domain(_: Request, e: DomainError):
        return JSONResponse(e.body(), status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query"))
        return _error(422, "invalid_input", f"{where}: {first.get('msg', 'invalid')}" if where else first.get("msg", "invalid input"))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        return _error(e.status_code, "http_error", str(e.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, e: Exception):
        log.exception("unhandled error")
        return _error(500, "internal_error", "Something went wrong on our side.")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "service": label}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        return {"ok": True, "service": label, "mode": st.mode}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    for m in mods:
        if hasattr(m, "router"):
            app.include_router(m.router, dependencies=internal)
        if hasattr(m, "public"):
            app.include_router(m.public)

    if st.is_local and os.path.isdir(st.web_dist):
        _mount_spa(app, st.web_dist)
    return app


def _mount_spa(app: FastAPI, dist: str) -> None:
    """Local mode serves the built web app from the same process; any unknown path returns index.html."""
    index = os.path.join(dist, "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "internal/", "hooks/", "admin/api/")):
            return _error(404, "not_found", "No such endpoint.")
        full = os.path.realpath(os.path.join(dist, path))
        if path and full.startswith(os.path.realpath(dist) + os.sep) and os.path.isfile(full):
            return FileResponse(full)
        return FileResponse(index, headers={"cache-control": "no-cache"})
