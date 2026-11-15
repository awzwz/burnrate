"""identity: visitors, accounts, sign-in links, consents. Never stores card numbers, bank logins or SSNs."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus
from burnrate.common.auth import hash_token, new_link_token
from burnrate.common.config import settings
from burnrate.common.context import real_now
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.common.errors import NotFound, Unauthorized

SVC = "identity"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/identity", tags=["identity"])


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    anonymous: Mapped[bool] = mapped_column(default=True)
    demo: Mapped[bool] = mapped_column(default=False)
    tz: Mapped[str] = mapped_column(String(64), default="")
    merged_into: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LoginToken(Base):
    __tablename__ = "login_tokens"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Consent(Base):
    __tablename__ = "consents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # terms | auto_renewal | sms
    text: Mapped[str] = mapped_column(String(600))  # exactly what the person agreed to
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


def view(u: User) -> dict:
    return {"id": u.id, "email": u.email, "anonymous": u.anonymous, "demo": u.demo,
            "tz": u.tz or settings().default_timezone, "created_at": u.created_at.isoformat()}


class NewUser(BaseModel):
    demo: bool = False
    tz: str = ""


@router.post("/users")
async def create_user(req: NewUser):
    async with session(SVC) as s:
        u = User(id=new_id(), email=None, anonymous=True, demo=req.demo, tz=req.tz, created_at=real_now())
        s.add(u)
        return view(u)


@router.get("/users/{user_id}")
async def get_user(user_id: str):
    async with session(SVC) as s:
        u = await s.get(User, user_id)
        if not u or u.deleted_at:
            raise NotFound("user not found", code="user_not_found")
        return view(u)


class EmailReq(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)


@router.post("/users/{user_id}/email")
async def attach_email(user_id: str, req: EmailReq):
    """Turn an anonymous visitor into an account. If the email already has an account, that account wins."""
    email = req.email.strip().lower()
    async with session(SVC) as s:
        u = await s.get(User, user_id)
        if not u:
            raise NotFound("user not found", code="user_not_found")
        other = (await s.execute(select(User).where(User.email == email, User.id != user_id, User.deleted_at.is_(None)))).scalar_one_or_none()
        if other:
            u.merged_into = other.id
            return {"user": view(other), "switched": True}
        u.email, u.anonymous = email, False
        return {"user": view(u), "switched": False}


@router.patch("/users/{user_id}")
async def update_user(user_id: str, req: dict):
    async with session(SVC) as s:
        u = await s.get(User, user_id)
        if not u:
            raise NotFound("user not found", code="user_not_found")
        if "tz" in req:
            u.tz = str(req["tz"])[:64]
        return view(u)


@router.post("/link")
async def create_link(req: EmailReq):
    """A one-time sign-in token for an existing account. Unknown emails get the same answer and no token."""
    email = req.email.strip().lower()
    async with session(SVC) as s:
        u = (await s.execute(select(User).where(User.email == email, User.deleted_at.is_(None)))).scalar_one_or_none()
        if not u:
            return {"token": None, "user_id": None}
        raw, hashed = new_link_token()
        s.add(LoginToken(hash=hashed, user_id=u.id, expires_at=real_now() + dt.timedelta(minutes=30)))
        return {"token": raw, "user_id": u.id}


class VerifyReq(BaseModel):
    token: str


@router.post("/verify")
async def verify(req: VerifyReq):
    async with session(SVC) as s:
        t = await s.get(LoginToken, hash_token(req.token))
        if not t or t.used_at or t.expires_at < real_now():
            raise Unauthorized("This sign-in link has expired. Ask for a new one.", code="link_expired")
        t.used_at = real_now()
        u = await s.get(User, t.user_id)
        return view(u)


class ConsentReq(BaseModel):
    kind: str = Field(max_length=40)
    text: str = Field(max_length=600)


@router.post("/users/{user_id}/consents")
async def add_consent(user_id: str, req: ConsentReq):
    async with session(SVC) as s:
        s.add(Consent(user_id=user_id, kind=req.kind, text=req.text, at=real_now()))
    return {"ok": True}


@router.get("/users/{user_id}/consents")
async def list_consents(user_id: str):
    async with session(SVC) as s:
        rows = (await s.execute(select(Consent).where(Consent.user_id == user_id).order_by(Consent.at))).scalars().all()
        return {"consents": [{"kind": c.kind, "text": c.text, "at": c.at.isoformat()} for c in rows]}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str):
    async with session(SVC) as s:
        u = await s.get(User, user_id)
        if not u:
            raise NotFound("user not found", code="user_not_found")
        u.deleted_at, u.email = real_now(), None
        await bus.emit(s, SVC, "user.deleted", {"user_id": user_id}, key=user_id)
    return {"ok": True}


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        rows = (await s.execute(select(User.anonymous, User.demo))).all()
    return {"users": len(rows), "accounts": sum(1 for a, d in rows if not a and not d), "demo": sum(1 for _, d in rows if d)}
