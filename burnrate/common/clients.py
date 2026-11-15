"""Calls between services. Same code in both modes: HTTP to `http://<service>:8000` in the cluster,
an in-process ASGI call in local mode. The request context (user, demo clock, request id) travels as headers."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from burnrate.common.config import settings
from burnrate.common.context import get_ctx
from burnrate.common.errors import DomainError, Unavailable

_local_app = None
_clients: dict[str, httpx.AsyncClient] = {}


def set_local_app(app) -> None:
    global _local_app
    _local_app = app
    _clients.clear()


def _client(service: str) -> httpx.AsyncClient:
    if service not in _clients:
        if settings().is_local:
            if _local_app is None:
                raise RuntimeError("local app is not set")
            _clients[service] = httpx.AsyncClient(transport=httpx.ASGITransport(app=_local_app), base_url="http://local")
        else:
            _clients[service] = httpx.AsyncClient(
                base_url=f"http://{service}:8000",
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            )
    return _clients[service]


async def call(service: str, method: str, path: str, *, json: Any = None, params: dict | None = None,
               timeout: float = 10.0, retries: int | None = None) -> Any:
    """Internal request. Raises DomainError with the remote code and status on failure."""
    headers = {**get_ctx().headers(), "x-internal-token": settings().internal_token}
    attempts = (2 if method.upper() == "GET" else 0) if retries is None else retries
    last: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            r = await _client(service).request(method, path, json=json, params=params, headers=headers, timeout=timeout)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            last = e
            await asyncio.sleep(0.1 * (attempt + 1))
            continue
        if r.status_code >= 500 and attempt < attempts:
            await asyncio.sleep(0.1 * (attempt + 1))
            continue
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except Exception:  # noqa: BLE001
                err = {}
            raise DomainError(err.get("message") or f"{service} answered {r.status_code}",
                              code=err.get("code") or "upstream_error", status=r.status_code, details=err.get("details"))
        return r.json() if r.content else None
    raise Unavailable(f"{service} is not reachable: {last}", code=f"{service}_unavailable")


async def get(service: str, path: str, **kw) -> Any:
    return await call(service, "GET", path, **kw)


async def post(service: str, path: str, json: Any = None, **kw) -> Any:
    return await call(service, "POST", path, json=json, **kw)


async def put(service: str, path: str, json: Any = None, **kw) -> Any:
    return await call(service, "PUT", path, json=json, **kw)


async def close_all() -> None:
    for c in _clients.values():
        await c.aclose()
    _clients.clear()
