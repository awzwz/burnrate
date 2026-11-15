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


def _stream_error(service: str, status: int, body: bytes) -> DomainError:
    try:
        import json as _json

        err = _json.loads(body).get("error", {})
    except Exception:  # noqa: BLE001
        err = {}
    return DomainError(err.get("message") or f"{service} answered {status}", code=err.get("code") or "upstream_error", status=status)


async def _local_stream(service: str, method: str, path: str, json: Any, headers: dict[str, str]):
    """Local mode: call the in-process app directly and hand over each line as soon as it is sent.
    httpx's ASGI transport returns the body only when it is complete, which would turn a stream into one late block."""
    import asyncio
    import contextlib
    import json as _json
    from urllib.parse import urlsplit

    parts = urlsplit(path)
    body = _json.dumps(json).encode() if json is not None else b""
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": method.upper(),
             "scheme": "http", "path": parts.path, "raw_path": parts.path.encode(), "query_string": parts.query.encode(),
             "root_path": "", "server": ("local", 80), "client": ("127.0.0.1", 0),
             "headers": [(b"host", b"local"), (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                         *[(k.lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in headers.items()]]}
    queue: asyncio.Queue = asyncio.Queue()
    gone = asyncio.Event()
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def run() -> None:
        try:
            await _local_app(scope, receive, queue.put)
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    status, pending, failure = 200, b"", b""
    try:
        while (message := await queue.get()) is not None:
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if status >= 400:
                    failure += chunk
                else:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for raw in lines:
                        if raw.strip():
                            yield raw.decode()
                if not message.get("more_body"):
                    break
        if status >= 400:
            raise _stream_error(service, status, failure)
        if pending.strip():
            yield pending.decode()
    finally:
        gone.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def stream_lines(service: str, method: str, path: str, *, json: Any = None, timeout: float = 120.0):
    """Yield the lines of a streamed internal response (NDJSON). Errors before the first byte raise DomainError."""
    headers = {**get_ctx().headers(), "x-internal-token": settings().internal_token}
    if settings().is_local and _local_app is not None:
        async for line in _local_stream(service, method, path, json, headers):
            yield line
        return
    async with _client(service).stream(method, path, json=json, headers=headers, timeout=timeout) as r:
        if r.status_code >= 400:
            raise _stream_error(service, r.status_code, await r.aread())
        async for line in r.aiter_lines():
            if line.strip():
                yield line


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
