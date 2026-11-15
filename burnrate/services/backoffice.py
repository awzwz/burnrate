"""backoffice: the operator's view. Read-only numbers from every service and the switch for the language model.
Guarded by an admin token; in the cluster it has no public route (reach it with a port-forward)."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from burnrate.common import clients
from burnrate.common.config import settings
from burnrate.common.context import Ctx, set_ctx
from burnrate.common.errors import DomainError, Forbidden

public = APIRouter(prefix="/admin/api", tags=["backoffice"])
STATS = {"identity": "/internal/identity/stats", "funnel": "/internal/funnel/stats", "cards": "/internal/cards/stats",
         "plan": "/internal/plan/stats", "billing": "/internal/billing/stats", "statements": "/internal/statements/stats",
         "notifications": "/internal/notifications/stats", "scheduler": "/internal/scheduler/stats", "demo": "/internal/demo/stats",
         "asks": "/internal/asks/stats"}


async def admin(request: Request) -> None:
    if not hmac.compare_digest(request.headers.get("x-admin-token", ""), settings().admin_token):
        raise Forbidden("admin token required", code="admin_only")
    set_ctx(Ctx(user_id="admin"))


@public.get("/overview", dependencies=[Depends(admin)])
async def overview():
    out: dict = {}
    for svc, path in STATS.items():
        try:
            out[svc] = await clients.get(svc, path)
        except DomainError as e:
            out[svc] = {"error": e.code}
    try:
        out["funnel_steps"] = await clients.get("analytics-api", "/internal/analytics/funnel")
        out["llm"] = await clients.get("llm-gateway", "/internal/llm/status")
    except DomainError as e:
        out["llm"] = {"error": e.code}
    return out


@public.get("/llm", dependencies=[Depends(admin)])
async def llm_status():
    return await clients.get("llm-gateway", "/internal/llm/status")


class LlmConfig(BaseModel):
    provider: str
    model: str | None = None


@public.put("/llm", dependencies=[Depends(admin)])
async def llm_config(req: LlmConfig):
    """Switch the language model provider at run time: openai (default), anthropic, or fake."""
    return await clients.put("llm-gateway", "/internal/llm/config", json=req.model_dump(exclude_none=True))


@public.get("/rules", dependencies=[Depends(admin)])
async def rules():
    return await clients.get("engine", "/internal/engine/rules")


@public.get("/playbook", dependencies=[Depends(admin)])
async def playbook(issuer: str = "", type: str = ""):
    return await clients.get("asks", "/internal/asks/playbook", params={"issuer": issuer, "type": type})
