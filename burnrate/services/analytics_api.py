"""analytics-api: product events. Accepts batches, publishes them, a sink stores them.
Demo sessions are marked so real and test traffic never mix in a report."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus
from burnrate.common.context import get_ctx, real_now
from burnrate.common.db import UTCDateTime, make_base, new_id, session

SVC = "analytics-api"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/analytics", tags=["analytics"])

FUNNEL = ("landing_view", "quiz_start", "quiz_card_added", "result_view", "paywall_view", "checkout_start", "purchase",
          "paid_marked", "statement_added", "ask_outcome_logged", "rehearsal_started", "cancel")


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(48), index=True)
    props: Mapped[dict] = mapped_column(JSON, default=dict)
    demo: Mapped[bool] = mapped_column(default=False)
    ts: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)


class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_.]+$")
    props: dict = {}
    ts: dt.datetime | None = None


class Batch(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=50)


@router.post("/events")
async def ingest(req: Batch):
    ctx = get_ctx()
    async with session(SVC) as s:
        await bus.emit(s, SVC, "analytics.events", {"user_id": ctx.user_id, "demo": ctx.demo, "events": [
            {"name": e.name, "props": {k: v for k, v in list(e.props.items())[:12] if isinstance(v, (str, int, float, bool))},
             "ts": (e.ts or real_now()).isoformat()} for e in req.events]})
    return {"accepted": len(req.events)}


@bus.subscribe("analytics.events", worker="analytics-sink", service=SVC)
async def sink(ev: bus.Event) -> None:
    p = ev.payload
    async with session(SVC) as s:
        for e in p["events"]:
            ts = dt.datetime.fromisoformat(e["ts"])
            s.add(EventRow(id=new_id(), user_id=p["user_id"], name=e["name"], props=e["props"], demo=bool(p.get("demo")),
                           ts=ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)))


@router.get("/funnel")
async def funnel(include_demo: bool = False):
    """Distinct users per funnel step."""
    async with session(SVC) as s:
        q = select(EventRow.name, func.count(func.distinct(EventRow.user_id))).group_by(EventRow.name)
        if not include_demo:
            q = q.where(EventRow.demo.is_(False))
        counts = dict((await s.execute(q)).all())
    return {"steps": [{"name": n, "users": counts.get(n, 0)} for n in FUNNEL], "other": {k: v for k, v in counts.items() if k not in FUNNEL}}
