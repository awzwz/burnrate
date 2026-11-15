"""Per-request context carried between services: who is asking, their clock, the request id.

The edge sets it from the session; internal calls forward it as headers (see clients.py), so every
service sees the same user and the same "now".
"""
from __future__ import annotations

import datetime as dt
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, replace
from zoneinfo import ZoneInfo

from burnrate.common.config import settings


@dataclass(frozen=True)
class Ctx:
    user_id: str = ""
    demo: bool = False
    clock_offset_s: int = 0  # demo clock: seconds added to real time
    tz: str = ""
    request_id: str = ""

    def headers(self) -> dict[str, str]:
        return {
            "x-user-id": self.user_id,
            "x-demo": "1" if self.demo else "0",
            "x-clock-offset": str(self.clock_offset_s),
            "x-user-tz": self.tz,
            "x-request-id": self.request_id or uuid.uuid4().hex,
        }

    @classmethod
    def from_headers(cls, h) -> "Ctx":
        def geti(name: str) -> int:
            try:
                return int(h.get(name) or 0)
            except ValueError:
                return 0

        return cls(
            user_id=h.get("x-user-id") or "",
            demo=(h.get("x-demo") == "1"),
            clock_offset_s=geti("x-clock-offset"),
            tz=h.get("x-user-tz") or "",
            request_id=h.get("x-request-id") or uuid.uuid4().hex,
        )


_ctx: ContextVar[Ctx] = ContextVar("burnrate_ctx", default=Ctx())


def get_ctx() -> Ctx:
    return _ctx.get()


def set_ctx(ctx: Ctx):
    return _ctx.set(ctx)


def reset_ctx(token) -> None:
    _ctx.reset(token)


def with_ctx(**changes) -> Ctx:
    return replace(_ctx.get(), **changes)


# ---------- clock: nobody calls datetime.now() directly ----------

def now() -> dt.datetime:
    """Current moment for this request (real time plus the demo offset), timezone-aware UTC."""
    return dt.datetime.now(dt.UTC) + dt.timedelta(seconds=_ctx.get().clock_offset_s)


def real_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def user_tz() -> ZoneInfo:
    return ZoneInfo(_ctx.get().tz or settings().default_timezone)


def today() -> dt.date:
    """The user's calendar date. Paydays, due dates and statement dates are local dates."""
    return now().astimezone(user_tz()).date()
