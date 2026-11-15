"""End-to-end journeys through the public API, all services in one process, every number computed by the engine."""
import datetime as dt

import pytest

from burnrate.common import persona

CORE = ["edge-api", "webhooks", "identity", "funnel", "cards", "engine", "plan", "statements", "billing", "notifications",
        "scheduler", "analytics-api", "demo", "backoffice"]


def available(*names: str) -> list[str]:
    """Core services plus optional ones that exist in this checkout."""
    import importlib

    from burnrate.services import SERVICES

    out = list(CORE)
    for n in names:
        try:
            importlib.import_module(SERVICES[n])
            out.append(n)
        except ModuleNotFoundError:
            pass
    return out


async def api(s, method: str, path: str, **kw):
    r = await s.client.request(method, "/api" + path, **kw)
    return r


async def ok(s, method: str, path: str, **kw) -> dict:
    r = await api(s, method, path, **kw)
    assert r.status_code < 300, f"{method} {path} -> {r.status_code} {r.text[:300]}"
    return r.json() if r.content else {}


# ---------- funnel to purchase ----------

async def test_visitor_goes_from_quiz_to_a_paid_plan(stack):
    s = await stack(available("guard", "asks", "llm-gateway", "rehearsal"))
    assert (await api(s, "GET", "/me")).status_code == 401
    me = await ok(s, "POST", "/session")
    assert me["user"]["anonymous"] and not me["entitled"] and me["subscription"] is None
    assert me["config"]["billing_provider"] == "demo" and me["config"]["test_card"] == "4242 4242 4242 4242"
    assert (await api(s, "GET", "/today")).status_code == 402  # the product is behind the plan

    live = await ok(s, "POST", "/quiz/card-burn", json={"balance_cents": 520_000, "apr_bps": 2799})
    assert round(live["burn_per_day"]) == 399

    assert (await api(s, "POST", "/quiz/result")).json()["error"]["code"] == "no_cards"
    quiz = await ok(s, "POST", "/quiz/sample")
    assert len(quiz["cards"]) == 3 and quiz["schedule"]["frequency"] == "biweekly"
    result = await ok(s, "POST", "/quiz/result")
    assert round(result["burn_per_day"]) == 738 and result["per_year_cents"] == 269_495
    assert result["first_move"]["amount_cents"] == 24_000 and result["first_move"]["card_name"] == "Capital One Quicksilver"
    assert [a["type"] for a in result["asks"]] == ["late_fee_refund", "apr_reduction", "apr_reduction"]
    again = await ok(s, "GET", "/quiz/result")
    assert again["id"] == result["id"] and again["burn_per_day"] == result["burn_per_day"]  # the number does not change

    plans = await ok(s, "GET", "/billing/plans")
    assert [p["id"] for p in plans["plans"]] == ["w1", "w4", "w12"] and plans["plans"][2]["then"] == "then $79.99 every 12 weeks"
    no = await api(s, "POST", "/billing/checkout", json={"plan_id": "w12", "email": "maya@example.com", "consent_renewal": False})
    assert no.status_code == 400 and no.json()["error"]["code"] == "consent_required"
    co = await ok(s, "POST", "/billing/checkout", json={"plan_id": "w12", "email": "maya@example.com", "consent_renewal": True})
    assert co["amount_cents"] == 3999 and co["url"] is None and co["test_mode"]

    declined = await api(s, "POST", f"/billing/checkout/{co['id']}/pay", json={"card_number": "4000 0000 0000 0002", "exp": "12/30", "cvc": "123"})
    assert declined.status_code == 402 and declined.json()["error"]["code"] == "card_declined"
    me = await ok(s, "POST", f"/billing/checkout/{co['id']}/pay", json={"card_number": "4242 4242 4242 4242", "exp": "12/30", "cvc": "123"})
    assert me["entitled"] and me["subscription"]["status"] == "active" and me["subscription"]["plan"]["id"] == "w12"
    assert me["user"]["email"] == "maya@example.com" and not me["user"]["anonymous"]
    assert me["subscription"]["paid_total_cents"] == 3999 and me["subscription"]["refundable_until"]

    today = await ok(s, "GET", "/today")
    assert len(today["cards"]) == 3 and round(today["burn"]["now"]) == 738
    assert today["instruction"]["sends"][0]["amount_cents"] == 24_000
    assert today["meter"]["paid_cents"] == 3999 and today["meter"]["found_cents"] == 0  # honest: nothing found yet

    inbox = (await ok(s, "GET", "/inbox"))["messages"]
    welcome = next(m for m in inbox if m["template"] == "welcome")
    assert "renews at $79.99 every 12 weeks" in welcome["body"] and welcome["to"] == "maya@example.com"

    second = await api(s, "POST", "/billing/checkout", json={"plan_id": "w4", "email": "maya@example.com", "consent_renewal": True})
    assert second.status_code == 409 and second.json()["error"]["code"] == "already_subscribed"


async def test_cancel_pause_and_refund(stack):
    s = await stack(available("guard"))
    await ok(s, "POST", "/session")
    await ok(s, "POST", "/quiz/sample")
    await ok(s, "POST", "/quiz/result")
    co = await ok(s, "POST", "/billing/checkout", json={"plan_id": "w4", "email": "a@example.com", "consent_renewal": True})
    await ok(s, "POST", f"/billing/checkout/{co['id']}/pay", json={"card_number": "4242424242424242", "exp": "01/31", "cvc": "999"})

    sub = await ok(s, "POST", "/billing/cancel", json={"reason": "too expensive"})
    assert sub["status"] == "active" and sub["cancel_at_period_end"]  # access stays until the paid period ends
    assert (await ok(s, "GET", "/me"))["entitled"]
    assert (await ok(s, "POST", "/billing/resume"))["cancel_at_period_end"] is False

    paused = await ok(s, "POST", "/billing/pause", json={"weeks": 2})
    assert paused["status"] == "paused" and paused["paused_until"]
    assert (await api(s, "GET", "/today")).status_code == 402
    assert (await ok(s, "POST", "/billing/resume"))["status"] == "active"

    refunded = await ok(s, "POST", "/billing/refund")
    assert refunded["status"] == "refunded" and refunded["paid_total_cents"] == 0
    assert not (await ok(s, "GET", "/me"))["entitled"]


# ---------- demo ----------

async def start_demo(s) -> dict:
    me = await ok(s, "POST", "/demo/start", json={})
    assert me["demo"]["persona"] == "aziz" and me["entitled"] and me["user"]["demo"]
    return me


async def test_demo_starts_on_a_payday_with_history(stack):
    s = await stack(available("guard", "asks", "llm-gateway", "rehearsal"))
    me = await start_demo(s)
    d0 = dt.date.fromisoformat(me["demo"]["started_on"])
    assert d0.weekday() == 4 and me["today"] == d0.isoformat()  # a Friday, and the clock says so

    t = await ok(s, "GET", "/today")
    ins = t["instruction"]
    assert t["headline"] == "It's payday." and ins["status"] == "due"
    send = ins["sends"][0]
    assert (send["card_name"], send["amount_cents"], send["covers_min"], send["extra_cents"]) == ("Capital One Quicksilver", 24_000, True, 6_867)
    assert send["saved_by_date_cents"] == 350 and send["days_early"] == 19
    assert round(t["burn"]["now"]) == 738 and round(t["burn"]["start"]) == 794
    assert [round(p["value"]) for p in t["burn"]["history"]] == [794, 775, 757, 738]
    assert t["burn"]["history"][2]["label"] == "Chase lowered your rate to 22.49%"
    assert {(a["card_name"], a["source"]) for a in t["autopay"]} == {
        ("Discover it", "this_paycheck"), ("Chase Freedom Unlimited", "next_paycheck"), ("Capital One Quicksilver", "covered_by_payment")}
    assert t["meter"]["paid_cents"] == 3999 and t["meter"]["estimated_cents"] == 700

    sms = [m for m in (await ok(s, "GET", "/inbox"))["messages"] if m["channel"] == "sms"]
    assert len(sms) == 1 and "Send $240.00 to Capital One Quicksilver today" in sms[0]["body"] and sms[0]["to"] == persona.PHONE

    stmts = (await ok(s, "GET", "/statements"))["statements"]
    assert len(stmts) == 1 and stmts[0]["status"] == "reconciled" and stmts[0]["reconciliation"]["diff_cents"] == -8


async def test_mark_as_paid_undo_and_the_next_payday(stack):
    s = await stack(available("guard", "asks", "llm-gateway", "rehearsal"))
    await start_demo(s)
    t = await ok(s, "GET", "/today")
    ins_id = t["instruction"]["id"]

    paid = await ok(s, "POST", f"/plan/instructions/{ins_id}/paid", json={})
    assert paid["headline"] == "Done for this payday." and paid["instruction"]["status"] == "paid"
    assert round(paid["burn"]["now"]) == 720 and paid["instruction"]["paid_cents"] == 24_000
    assert paid["burn"]["history"][-1]["label"] == "Payday, $240 to Capital One Quicksilver"
    assert next(c for c in paid["cards"] if c["name"] == "Capital One Quicksilver")["balance_cents"] == 496_000
    assert paid["meter"]["estimated_cents"] == 1_050
    twice = await ok(s, "POST", f"/plan/instructions/{ins_id}/paid", json={})
    assert next(c for c in twice["cards"] if c["name"] == "Capital One Quicksilver")["balance_cents"] == 496_000  # not paid twice

    undone = await ok(s, "POST", f"/plan/instructions/{ins_id}/undo")
    assert undone["instruction"]["status"] == "due" and round(undone["burn"]["now"]) == 738 and len(undone["burn"]["history"]) == 4
    await ok(s, "POST", f"/plan/instructions/{ins_id}/paid", json={})

    jump = await ok(s, "POST", "/demo/clock", json={"action": "next_payday"})
    d0 = dt.date.fromisoformat(jump["me"]["demo"]["started_on"])
    assert jump["jumped_to"] == (d0 + dt.timedelta(days=14)).isoformat() and jump["me"]["today"] == jump["jumped_to"]
    t2 = await ok(s, "GET", "/today")
    assert t2["headline"] == "It's payday." and t2["instruction"]["id"] != ins_id
    send = t2["instruction"]["sends"][0]
    assert send["card_name"] == "Capital One Quicksilver" and send["amount_cents"] == 30_000 - t2["instruction"]["reserved_cents"]
    assert ("Chase Freedom Unlimited", "this_paycheck") in {(a["card_name"], a["source"]) for a in t2["autopay"]}
    sms = [m for m in (await ok(s, "GET", "/inbox"))["messages"] if m["channel"] == "sms"]
    assert len(sms) == 2  # one text per payday, never a duplicate


async def test_short_paycheck_and_new_charge(stack):
    s = await stack(available("guard"))
    await start_demo(s)
    t = await ok(s, "GET", "/today")
    short = await ok(s, "POST", f"/plan/instructions/{t['instruction']['id']}/short", json={"available_cents": 10_000})
    assert short["instruction"]["sends"][0]["amount_cents"] == 4_000 and short["instruction"]["short_by_cents"] == 0
    cap = next(c for c in t["cards"] if c["name"] == "Capital One Quicksilver")
    after = await ok(s, "POST", f"/cards/{cap['id']}/charges", json={"amount_cents": 12_345, "note": "Car repair"})
    assert after["balance_cents"] == 532_345
    t2 = await ok(s, "GET", "/today")
    assert t2["burn"]["now"] > t["burn"]["now"] and t2["burn"]["history"][-1]["label"] == "New purchase on Capital One Quicksilver"


async def test_cards_refinance_projection_and_calendar(stack):
    s = await stack(available("guard"))
    await start_demo(s)
    cards = await ok(s, "GET", "/cards")
    assert round(cards["burn_per_day"]) == 738 and cards["total_balance_cents"] == 1_050_000
    cap = next(c for c in cards["cards"] if c["name"] == "Capital One Quicksilver")
    assert cap["min_payment_cents"] == 17_133 and 0.08 < cap["progress"] < 0.09 and cap["pay_url"].endswith("capitalone.com")
    ref = await ok(s, "GET", f"/cards/{cap['id']}/refinance", params={"monthly_payment_cents": 30_000})
    assert ref["transfer"]["fee_3"]["fee_cents"] == 15_600 and ref["transfer"]["fee_3"]["saving_cents"] > 0 and len(ref["loans"]) == 4
    proj = await ok(s, "GET", "/plan/projection")
    assert proj["avalanche"]["months"] <= proj["snowball"]["months"] and proj["monthly_budget_cents"] == 65_000
    prefs = await ok(s, "GET", "/notifications/prefs")
    assert prefs["sms_opt_in"] and prefs["calendar_url"]
    path = prefs["calendar_url"].split("/api", 1)[1]
    ics = await s.client.get("/api" + path)
    assert ics.status_code == 200 and "BEGIN:VCALENDAR" in ics.text and "SUMMARY:Payday" in ics.text
    assert (await s.client.get("/api/calendar.ics", params={"u": "someone", "sig": "bad"})).status_code == 403


# ---------- statements ----------

async def test_manual_statement_is_checked_and_reconciled(stack):
    s = await stack(available("guard"))
    me = await start_demo(s)
    d0 = dt.date.fromisoformat(me["demo"]["started_on"])
    disc = next(c for c in (await ok(s, "GET", "/cards"))["cards"] if c["name"] == "Discover it")
    close = d0 - dt.timedelta(days=3)
    wrong = {"previous_balance_cents": 190_000, "payments_credits_cents": 6_000, "purchases_cents": 0, "fees_cents": 0,
             "interest_cents": 3_903, "new_balance_cents": 197_903, "apr_bps": 2499, "balance_subject_to_interest_cents": 190_000,
             "days_in_cycle": 30, "min_payment_cents": 6_000, "close_date": close.isoformat(),
             "due_date": (close + dt.timedelta(days=25)).isoformat()}
    st = await ok(s, "POST", "/statements/manual", json={"card_id": disc["id"], "fields": wrong})
    assert st["status"] == "needs_confirmation" and "new_balance_cents" in st["confirm_fields"]
    assert next(c for c in st["checks"] if c["name"] == "balance_adds_up")["ok"] is False

    fixed = await ok(s, "POST", f"/statements/{st['id']}/confirm", json={"fields": {"new_balance_cents": 187_903}})
    assert fixed["status"] == "reconciled" and fixed["reconciliation"]["actual_interest_cents"] == 3_903
    assert fixed["reconciliation"]["summary"]
    after = next(c for c in (await ok(s, "GET", "/cards"))["cards"] if c["name"] == "Discover it")
    assert after["balance_cents"] == 187_903 and after["min_payment_cents"] == 6_000  # the statement wins
    mail = [m for m in (await ok(s, "GET", "/inbox"))["messages"] if m["template"] == "statement_ready"]
    assert len(mail) == 1 and "Discover it" in mail[0]["subject"]


async def test_statement_with_a_late_fee_and_a_rate_increase_raises_triggers(stack):
    s = await stack(available("guard"))
    me = await start_demo(s)
    d0 = dt.date.fromisoformat(me["demo"]["started_on"])
    chase = next(c for c in (await ok(s, "GET", "/cards"))["cards"] if c["name"] == "Chase Freedom Unlimited")
    close = d0 - dt.timedelta(days=9)  # due 25 days later, on the card's usual due day
    fields = {"new_balance_cents": 350_300, "interest_cents": 6_400, "fees_cents": 3_900, "late_fee_cents": 3_900, "apr_bps": 2274,
              "min_payment_cents": 13_803, "close_date": close.isoformat(), "due_date": (close + dt.timedelta(days=25)).isoformat()}
    st = await ok(s, "POST", "/statements/manual", json={"card_id": chase["id"], "fields": fields})
    assert st["status"] == "reconciled"
    assert [t["type"] for t in st["triggers"]] == ["late_fee_refund", "apr_reduction"]
    assert "22.49% to 22.74%" in st["triggers"][1]["detail"]
    after = next(c for c in (await ok(s, "GET", "/cards"))["cards"] if c["id"] == chase["id"])
    assert after["apr_bps"] == 2274 and after["balance_cents"] == 350_300


@pytest.mark.parametrize("name", ["llm-gateway"])
async def test_demo_next_statement_runs_the_real_pipeline(stack, name):
    services = available("guard", "asks", "llm-gateway", "rehearsal")
    if name not in services:
        pytest.skip("llm-gateway is not in this checkout yet")
    s = await stack(services)
    await start_demo(s)
    jump = await ok(s, "POST", "/demo/clock", json={"action": "next_statement"})
    assert jump["statement_id"] and "statement is out" in jump["note"]
    st = await ok(s, "GET", f"/statements/{jump['statement_id']}")
    assert st["status"] == "reconciled" and st["source"] == "demo"
    rec = st["reconciliation"]
    assert rec["diff_cents"] != 0 and rec["parts"][0]["label"] == "New purchases on the card" and rec["within_tolerance"]
    assert st["fields"]["purchases_cents"] == 8_640


# ---------- access control and operations ----------

async def test_one_user_cannot_read_another_users_data(stack):
    s = await stack(available("guard"))
    await start_demo(s)
    t = await ok(s, "GET", "/today")
    card_id, ins_id = t["cards"][0]["id"], t["instruction"]["id"]
    s.client.cookies.clear()
    await ok(s, "POST", "/demo/start", json={})  # a second guest
    assert (await api(s, "GET", f"/cards/{card_id}/ledger")).status_code == 404
    assert (await api(s, "POST", f"/plan/instructions/{ins_id}/paid", json={})).status_code == 404
    assert (await api(s, "POST", f"/cards/{card_id}/charges", json={"amount_cents": 100})).status_code == 404


async def test_internal_and_admin_endpoints_are_closed_to_the_public(stack):
    s = await stack(available("guard"))
    assert (await s.client.get("/internal/engine/rules")).status_code == 403
    assert (await s.client.get("/internal/cards")).status_code == 403
    assert (await s.client.get("/admin/api/overview")).status_code == 403
    forged = await s.client.get("/internal/cards", headers={"x-user-id": "someone", "x-internal-token": "guess"})
    assert forged.status_code == 403
    ov = await s.client.get("/admin/api/overview", headers={"x-admin-token": "dev-admin-token"})
    assert ov.status_code == 200 and "billing" in ov.json()
    assert (await ok(s, "GET", "/health"))["ok"] is True


async def test_stripe_webhook_signature():
    import hashlib
    import hmac
    import time

    from burnrate.services.billing import verify_stripe_signature

    body, secret, ts = b'{"id":"evt_1","type":"checkout.session.completed"}', "whsec_test", str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert verify_stripe_signature(body, f"t={ts},v1={sig}", secret)
    assert not verify_stripe_signature(body + b" ", f"t={ts},v1={sig}", secret)
    assert not verify_stripe_signature(body, f"t={int(ts) - 4000},v1={sig}", secret)
    assert not verify_stripe_signature(body, "garbage", secret)


async def test_analytics_events_are_stored_and_demo_traffic_is_kept_apart(stack):
    s = await stack(available("guard"))
    await ok(s, "POST", "/session")
    r = await api(s, "POST", "/events", json={"events": [{"name": "quiz_start"}, {"name": "result_view", "props": {"cards": 3}}]})
    assert r.status_code == 202
    await s.drain()
    await start_demo(s)
    await api(s, "POST", "/events", json={"events": [{"name": "quiz_start"}]})
    await s.drain()
    real = (await s.internal("GET", "/internal/analytics/funnel")).json()
    with_demo = (await s.internal("GET", "/internal/analytics/funnel", params={"include_demo": True})).json()
    step = lambda d, n: next(x["users"] for x in d["steps"] if x["name"] == n)
    assert step(real, "quiz_start") == 1 and step(with_demo, "quiz_start") == 2 and step(real, "result_view") == 1
