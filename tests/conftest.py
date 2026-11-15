"""Shared test fixtures: the whole platform (or a subset of services) in one process on throwaway SQLite files.

    async def test_x(stack):
        s = await stack(["cards", "engine"])            # or stack() for every service
        r = await s.internal("POST", "/internal/cards", json={...}, user="u1")
        await s.drain()                                   # deliver pending events to their handlers
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from burnrate.common import bus, clients, db
from burnrate.common.config import reset_settings, settings
from burnrate.common.context import Ctx


class Stack:
    def __init__(self, app, client: httpx.AsyncClient):
        self.app = app
        self.client = client  # public requests, cookie jar included

    async def internal(self, method: str, path: str, *, user: str = "", demo: bool = False, clock_offset_s: int = 0,
                       tz: str = "", **kw) -> httpx.Response:
        """Call an internal endpoint the way another service would."""
        headers = {**Ctx(user_id=user, demo=demo, clock_offset_s=clock_offset_s, tz=tz).headers(),
                   "x-internal-token": settings().internal_token, **kw.pop("headers", {})}
        return await self.client.request(method, path, headers=headers, **kw)

    async def drain(self) -> int:
        return await bus.drain()


def offset_to(date: dt.date, hour: int = 12) -> int:
    """Clock offset (seconds) that makes "now" the given UTC date."""
    target = dt.datetime(date.year, date.month, date.day, hour, tzinfo=dt.UTC)
    return int((target - dt.datetime.now(dt.UTC)).total_seconds())


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("BR_ENV", "test")
    monkeypatch.setenv("BR_MODE", "local")
    monkeypatch.setenv("BR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BR_WEB_DIST", str(tmp_path / "no-web"))
    monkeypatch.setenv("BR_LLM_PROVIDER", "fake")
    monkeypatch.setenv("BR_DATABASE_URL", "")
    monkeypatch.setenv("BR_KAFKA_BOOTSTRAP", "")
    monkeypatch.setenv("BR_REDIS_URL", "")
    reset_settings()
    opened: list = []

    async def make(names: list[str] | None = None) -> Stack:
        from burnrate.services import load_all
        from burnrate.services.base import make_app

        if names is None:
            load_all()
        app = make_app(names)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        opened.append((ctx, client))
        return Stack(app, client)

    yield make
    for ctx, client in opened:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
    await clients.close_all()
    await db.dispose_all()
    reset_settings()
