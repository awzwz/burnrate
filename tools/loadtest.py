"""Load test against a running Burn Rate deployment.

    python tools/loadtest.py --base https://burnrate.example --users 50 --seconds 30 --rps 200

Each virtual user is a quiz visitor with its own session: fills the quiz with sample numbers once, then
repeats the calls a real funnel makes (live card line, result, plans). `--mix app` adds a demo subscriber
loop (today, cards). Reports throughput, latency percentiles and errors. No paid API is touched.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time

import httpx

FUNNEL = [("POST", "/api/quiz/card-burn", {"balance_cents": 520_000, "apr_bps": 2799}), ("GET", "/api/quiz", None),
          ("POST", "/api/quiz/result", None), ("GET", "/api/billing/plans", None), ("GET", "/api/quiz/result", None),
          ("GET", "/api/me", None)]
APP = [("GET", "/api/today", None), ("GET", "/api/cards", None), ("GET", "/api/me", None), ("GET", "/api/plan/projection", None)]


async def user(base: str, mix: str, stop_at: float, gate: asyncio.Semaphore | None, interval: float, out: list, errors: dict) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=20, headers={"x-timezone": "America/New_York"}) as c:
        try:
            if mix == "app":
                r = await c.post("/api/demo/start", json={})
            else:
                await c.post("/api/session")
                r = await c.post("/api/quiz/sample")
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            errors[f"setup:{type(e).__name__}"] = errors.get(f"setup:{type(e).__name__}", 0) + 1
            return
        calls = APP if mix == "app" else FUNNEL
        while time.perf_counter() < stop_at:
            method, path, body = random.choice(calls)
            started = time.perf_counter()
            try:
                r = await c.request(method, path, json=body)
                elapsed = time.perf_counter() - started
                if r.status_code >= 400:
                    errors[str(r.status_code)] = errors.get(str(r.status_code), 0) + 1
                else:
                    out.append(elapsed)
            except Exception as e:  # noqa: BLE001
                errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
            if interval:
                await asyncio.sleep(max(0.0, interval - (time.perf_counter() - started)))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--rps", type=float, default=0, help="target total requests per second (0 = as fast as possible)")
    ap.add_argument("--mix", choices=("funnel", "app"), default="funnel")
    a = ap.parse_args()
    interval = a.users / a.rps if a.rps else 0.0
    out: list[float] = []
    errors: dict[str, int] = {}
    started = time.perf_counter()
    stop_at = started + a.seconds
    await asyncio.gather(*(user(a.base, a.mix, stop_at, None, interval, out, errors) for _ in range(a.users)))
    took = time.perf_counter() - started
    if not out:
        print("no successful requests", errors)
        return
    out.sort()
    q = lambda p: out[min(len(out) - 1, int(len(out) * p))] * 1000
    total_err = sum(errors.values())
    print(f"mix={a.mix} users={a.users} seconds={took:.1f}")
    print(f"requests ok={len(out)} errors={total_err} ({100 * total_err / (len(out) + total_err):.2f}%) {errors if errors else ''}")
    print(f"throughput {len(out) / took:.1f} req/s")
    print(f"latency ms: p50={q(.50):.0f} p90={q(.90):.0f} p95={q(.95):.0f} p99={q(.99):.0f} max={out[-1] * 1000:.0f} mean={statistics.mean(out) * 1000:.0f}")


if __name__ == "__main__":
    asyncio.run(main())
