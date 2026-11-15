"""guard: the wording rules of spec section 3.16 over HTTP. Stateless; the rules live in common/guardrails.py
so services that generate text can also call them in process."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from burnrate.common.guardrails import RULE_DESCRIPTIONS, RULES, check_text

router = APIRouter(prefix="/internal/guard", tags=["guard"])


class CheckReq(BaseModel):
    text: str = Field(max_length=100_000)
    allowed_numbers: list[str] | None = None


@router.post("/check")
async def check(req: CheckReq):
    allowed = set(req.allowed_numbers) if req.allowed_numbers is not None else None
    return check_text(req.text, allowed_numbers=allowed).model_dump()


@router.get("/rules")
async def rules():
    return {"rules": [{"name": name, "description": RULE_DESCRIPTIONS[name]} for name in RULES]}
