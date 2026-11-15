"""Service registry. One image; the SERVICE environment variable picks what a process runs.

Online services expose an internal router (`router`, reachable only with the internal token) and, for the edge
and the webhook receiver, a public router (`public`). Event handlers are declared with `@subscribe(..., worker=...)`
inside the owning service module and run in the worker deployment of that name.
"""
from __future__ import annotations

import importlib
from types import ModuleType

SERVICES: dict[str, str] = {
    "edge-api": "burnrate.services.edge_api",
    "webhooks": "burnrate.services.webhooks",
    "identity": "burnrate.services.identity",
    "funnel": "burnrate.services.funnel",
    "cards": "burnrate.services.cards",
    "engine": "burnrate.services.engine_svc",
    "plan": "burnrate.services.plan",
    "statements": "burnrate.services.statements",
    "asks": "burnrate.services.asks",
    "rehearsal": "burnrate.services.rehearsal",
    "llm-gateway": "burnrate.services.llm_gateway",
    "guard": "burnrate.services.guard",
    "billing": "burnrate.services.billing",
    "notifications": "burnrate.services.notifications",
    "scheduler": "burnrate.services.scheduler",
    "analytics-api": "burnrate.services.analytics_api",
    "demo": "burnrate.services.demo",
    "backoffice": "burnrate.services.backoffice",
}

# Kafka consumers (cluster mode). Each name is a deployment and a consumer group.
WORKERS: tuple[str, ...] = (
    "onboarding", "plan-events", "plan-recalc", "statement-extractor", "reconciler", "asks-events",
    "playbook-aggregator", "notifier", "billing-events", "analytics-sink",
)
# Loops that are not consumers.
LOOPS: tuple[str, ...] = ("timer-dispatcher",)
# Run-once jobs.
JOBS: tuple[str, ...] = ("schema", "retention")


def load(name: str) -> ModuleType:
    return importlib.import_module(SERVICES[name])


def load_all() -> dict[str, ModuleType]:
    return {name: load(name) for name in SERVICES}
