"""Single entrypoint for the single image: `SERVICE=<name> python -m burnrate.run`.

online service   -> uvicorn on :8000
worker           -> Kafka consume loop for the handlers registered under that worker name
timer-dispatcher -> the scheduler tick loop
schema/retention -> run once and exit
BR_MODE=local    -> every service, handler and loop in one process (development, tests, demos)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from burnrate.common import bus, db
from burnrate.common.config import settings
from burnrate.services import JOBS, LOOPS, SERVICES, WORKERS, load_all

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("burnrate.run")


def build_app():
    """ASGI factory used by uvicorn (`burnrate.run:build_app`)."""
    from burnrate.services.base import make_app

    st = settings()
    if st.is_local or st.service in ("all", ""):
        load_all()
        return make_app(None, service_label="all")
    return make_app([st.service])


async def _run_worker(name: str) -> None:
    load_all()  # registers subscriptions and database bases
    for svc in {s.service for s in bus.subscriptions(name)}:
        await db.create_all(svc)
    await bus.kafka_consume_loop(name)


async def _run_loop(name: str) -> None:
    load_all()
    if name == "timer-dispatcher":
        from burnrate.services import scheduler

        await db.create_all("scheduler")
        relay = asyncio.create_task(bus.kafka_relay_loop("scheduler"))
        try:
            await scheduler.tick_loop()
        finally:
            relay.cancel()


async def _run_job(name: str) -> None:
    load_all()
    if name == "schema":
        for svc in db.known_services():
            await db.create_all(svc)
            log.info("schema ready: %s", svc)
    elif name == "retention":
        from burnrate.services import statements

        removed = await statements.purge_files()
        log.info("retention: removed %s stored statement files", removed)
    await db.dispose_all()


def main() -> None:
    service = os.environ.get("SERVICE") or (sys.argv[1] if len(sys.argv) > 1 else "all")
    os.environ["BR_SERVICE"] = service
    st = settings()
    if service in SERVICES or service == "all" or st.is_local:
        import uvicorn

        uvicorn.run("burnrate.run:build_app", factory=True, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                    log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
    elif service in WORKERS:
        asyncio.run(_run_worker(service))
    elif service in LOOPS:
        asyncio.run(_run_loop(service))
    elif service in JOBS:
        asyncio.run(_run_job(service))
    else:
        raise SystemExit(f"unknown SERVICE: {service}")


if __name__ == "__main__":
    main()
