import datetime as dt

from burnrate.common import bus, db

CARD = {"name": "Capital One Quicksilver", "balance_cents": 520_000, "apr_bps": 2799, "close_day": 11, "due_day": 9,
        "statement_min_cents": 17_133, "opened_on": "2026-11-01"}


async def test_internal_endpoints_refuse_callers_without_the_token(stack):
    s = await stack(["cards", "engine"])
    r = await s.client.get("/internal/cards")
    assert r.status_code == 403 and r.json()["error"]["code"] == "internal_only"
    assert (await s.client.get("/healthz")).json()["ok"] is True


async def test_card_balance_is_the_sum_of_dated_ledger_events(stack):
    s = await stack(["cards", "engine"])
    card = (await s.internal("POST", "/internal/cards", json=CARD, user="u1")).json()
    assert card["issuer"] == "capital_one" and card["balance_cents"] == 520_000 and card["pay_url"].endswith("capitalone.com")
    cid = card["id"]
    r = await s.internal("POST", f"/internal/cards/{cid}/events", user="u1",
                         json={"type": "payment", "amount_cents": 24_000, "effective_date": "2026-11-20", "ref": "ins-1"})
    assert r.json()["duplicate"] is False
    again = await s.internal("POST", f"/internal/cards/{cid}/events", user="u1",
                             json={"type": "payment", "amount_cents": 24_000, "effective_date": "2026-11-20", "ref": "ins-1"})
    assert again.json()["duplicate"] is True  # the same reference is the same payment
    before = (await s.internal("GET", "/internal/cards", params={"on": "2026-11-19"}, user="u1")).json()["cards"][0]
    after = (await s.internal("GET", "/internal/cards", params={"on": "2026-11-20"}, user="u1")).json()["cards"][0]
    assert (before["balance_cents"], after["balance_cents"]) == (520_000, 496_000)
    assert after["progress"] == round(1 - 496_000 / 520_000, 4)
    undone = await s.internal("DELETE", f"/internal/cards/{cid}/events/by-ref/ins-1", user="u1")
    assert undone.json()["removed"] == 1 and undone.json()["card"]["balance_cents"] == 520_000


async def test_cards_are_private_to_their_owner(stack):
    s = await stack(["cards", "engine"])
    cid = (await s.internal("POST", "/internal/cards", json=CARD, user="u1")).json()["id"]
    assert (await s.internal("GET", "/internal/cards", user="u2")).json()["cards"] == []
    r = await s.internal("PATCH", f"/internal/cards/{cid}", json={"name": "Mine now"}, user="u2")
    assert r.status_code == 404 and r.json()["error"]["code"] == "card_not_found"


async def test_statement_true_up_adjusts_the_ledger_and_takes_the_rate(stack):
    s = await stack(["cards", "engine"])
    cid = (await s.internal("POST", "/internal/cards", json=CARD, user="u1")).json()["id"]
    r = (await s.internal("POST", f"/internal/cards/{cid}/true-up", user="u1",
                          json={"balance_cents": 531_933, "on": "2026-12-11", "statement_id": "st1", "apr_bps": 2824,
                                "statement_min_cents": 17_250})).json()
    assert r["adjusted_by_cents"] == 11_933 and r["rate_changed"] and r["old_apr_bps"] == 2799
    assert r["card"]["apr_bps"] == 2824 and r["card"]["statement_min_cents"] == 17_250
    twice = (await s.internal("POST", f"/internal/cards/{cid}/true-up", user="u1",
                              json={"balance_cents": 531_933, "on": "2026-12-11", "statement_id": "st1"})).json()
    assert twice["adjusted_by_cents"] == 0
    types = [e["type"] for e in (await s.internal("GET", f"/internal/cards/{cid}/ledger", user="u1")).json()["events"]]
    assert types == ["opening", "adjustment", "rate_change"]


async def test_every_ledger_change_publishes_an_event_through_the_outbox(stack):
    s = await stack(["cards", "engine"])
    seen: list[str] = []

    @bus.subscribe("card.ledger.changed", worker="test-listener", service="cards")
    async def listener(ev):
        seen.append(ev.payload["reason"])

    try:
        cid = (await s.internal("POST", "/internal/cards", json=CARD, user="u1")).json()["id"]
        await s.internal("POST", f"/internal/cards/{cid}/rate", json={"apr_bps": 2199}, user="u1")
        assert await s.drain() == 2 and seen == ["card_added", "rate_change"]
        assert await s.drain() == 0  # delivered once
    finally:
        bus._subs[:] = [x for x in bus._subs if x.worker != "test-listener"]
    assert "cards" in db.known_services()


async def test_engine_service_answers_with_the_persona_numbers(stack):
    s = await stack(["engine"])
    cards = [dict(CARD, id="cap"), {"id": "chase", "name": "Chase Freedom Unlimited", "issuer": "chase", "balance_cents": 340_000, "apr_bps": 2249, "close_day": 9, "due_day": 6},
             {"id": "disc", "name": "Discover it", "issuer": "discover", "balance_cents": 190_000, "apr_bps": 2499, "close_day": 30, "due_day": 27}]
    for c in cards:
        c.pop("opened_on", None)
    cards[0]["issuer"] = "capital_one"
    sched = {"frequency": "biweekly", "anchor": "2026-11-20", "per_payday_cents": 30_000}
    r = (await s.internal("POST", "/internal/engine/quiz-result", json={"cards": cards, "schedule": sched, "today": "2026-11-15"})).json()
    assert round(r["burn_per_day"]) == 738 and r["per_year_cents"] == 269_495 and r["per_paycheck_cents"] == 10_337
    assert r["first_move"]["amount_cents"] == 24_000 and r["first_move"]["card_name"] == "Capital One Quicksilver"
    assert r["first_move"]["payday"] == "2026-11-20" and r["cards"][0]["id"] == "cap"
    assert r["point_value"]["per_point_per_year_cents"] == 5_200
    bad = await s.internal("POST", "/internal/engine/quiz-result", json={"cards": [], "schedule": sched, "today": "2026-11-15"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_input"


async def test_identity_accounts_links_and_consents(stack):
    s = await stack(["identity"])
    u = (await s.internal("POST", "/internal/identity/users", json={})).json()
    assert u["anonymous"] and u["email"] is None
    r = (await s.internal("POST", f"/internal/identity/users/{u['id']}/email", json={"email": "Maya@Example.com"})).json()
    assert r["user"]["email"] == "maya@example.com" and not r["user"]["anonymous"] and not r["switched"]
    other = (await s.internal("POST", "/internal/identity/users", json={})).json()
    switched = (await s.internal("POST", f"/internal/identity/users/{other['id']}/email", json={"email": "maya@example.com"})).json()
    assert switched["switched"] and switched["user"]["id"] == u["id"]
    link = (await s.internal("POST", "/internal/identity/link", json={"email": "maya@example.com"})).json()
    assert (await s.internal("POST", "/internal/identity/verify", json={"token": link["token"]})).json()["id"] == u["id"]
    reused = await s.internal("POST", "/internal/identity/verify", json={"token": link["token"]})
    assert reused.status_code == 401 and reused.json()["error"]["code"] == "link_expired"
    assert (await s.internal("POST", "/internal/identity/link", json={"email": "nobody@example.com"})).json()["token"] is None
    await s.internal("POST", f"/internal/identity/users/{u['id']}/consents", json={"kind": "auto_renewal", "text": "I understand the plan renews at $79.99 every 12 weeks."})
    assert (await s.internal("GET", f"/internal/identity/users/{u['id']}/consents")).json()["consents"][0]["kind"] == "auto_renewal"
    assert isinstance(dt.datetime.fromisoformat(u["created_at"]), dt.datetime)
