# Burn Rate

A payday plan for people who carry credit-card balances. It shows what the cards cost per day and, on every payday, gives one instruction: one amount, one card, today.

Built for Zimran Business Cup 2.0 (Money track). Web only, B2C, US adults.

## What it does

- **Quiz.** Four numbers per card from the latest statement. The daily interest number appears before the paywall.
- **The asks (first 14 days).** What to ask each card issuer (lower rate, late-fee refund, annual fee, due-date move), a script with the user's numbers, a practice call with an AI bank rep, and an outcome log.
- **Payday plan.** Minimums stay on autopay. Everything above them goes to one card on payday.
- **Statement check.** Each month the forecast is compared with the bank's own figure and the ledger is corrected to it.
- **Paid-for-itself meter.** What the user paid for the product against what it found for them, shown even when negative.

The user makes every payment and every call. Burn Rate never moves money and never contacts a bank.

## How it is built

- **Engine** (`burnrate/engine`): deterministic interest math. Daily balance, APR ÷ 365, daily compounding, money in integer cents, rates in basis points, exact fractions. Pinned by golden tests.
- **Services** (`burnrate/services`): one image, the role is set by the `SERVICE` variable. Each service owns its database. Services talk over HTTP and through events (transactional outbox, idempotent consumers).
- **Language model** (`burnrate/common/llm`, `services/llm_gateway.py`): reads statements, plays the bank rep, explains. It never does arithmetic; code checks its numbers before anything is saved. OpenAI by default, Claude selectable, a fake provider for tests.
- **Web** (`web/`): React, Vite, TypeScript.
- **Deploy** (`deploy/`): Kubernetes manifests (Gateway API, Kafka, PostgreSQL per service, autoscaling, monitoring).

Public API contract: [docs/API.md](docs/API.md).

## Run locally

Everything runs in one process with SQLite files.

```bash
uv sync
cd web && npm ci && npm run build && cd ..
BR_MODE=local PORT=8000 .venv/bin/python -m burnrate.run
```

Open http://localhost:8000. The demo persona needs no login. Test card: `4242 4242 4242 4242`.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

Tests use the fake model provider and make no paid calls.
