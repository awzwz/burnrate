import datetime as dt

from burnrate.common import bus
from burnrate.common.db import session
from burnrate.common.guardrails import check_text
from tests.conftest import offset_to

STACK = ["asks", "cards", "engine", "llm-gateway", "guard", "rehearsal"]
DAY = dt.date(2026, 11, 20)
NOW = offset_to(DAY)
CAP = {"name": "Capital One Quicksilver", "balance_cents": 520_000, "apr_bps": 2799, "close_day": 11, "due_day": 9, "last4": "4821"}
CHASE = {"name": "Chase Freedom Unlimited", "balance_cents": 340_000, "apr_bps": 2249, "close_day": 9, "due_day": 6}
DISC = {"name": "Discover it", "balance_cents": 190_000, "apr_bps": 2499, "close_day": 30, "due_day": 27}
ANSWERS = {"late_fee_recently": True, "late_fee_card": "Discover it", "late_fee_cents": 2900, "late_fee_month": "October",
           "annual_fee_card": "Chase Freedom Unlimited", "autopay": "min"}
MONTHLY = {"frequency": "monthly", "anchor": "2026-12-15", "per_payday_cents": 60_000}
SURVEY_NUMBERS = {"84%", "6.3%", "92%", "94%"}


async def add_cards(s, user="u1", cards=(CAP, CHASE, DISC)) -> dict[str, str]:
    ids = {}
    for card in cards:
        r = await s.internal("POST", "/internal/cards", json={**card, "opened_on": "2026-11-01"}, user=user, clock_offset_s=NOW)
        assert r.status_code == 200, r.text
        ids[card["name"]] = r.json()["id"]
    return ids


async def call(s, method, path, user="u1", **kw):
    return await s.internal(method, f"/internal/asks{path}", user=user, clock_offset_s=kw.pop("offset", NOW), **kw)


async def built(s, user="u1", answers=ANSWERS, schedule=MONTHLY) -> list[dict]:
    r = await call(s, "POST", "/build", user=user, json={"answers": answers, "schedule": schedule})
    assert r.status_code == 200, r.text
    return r.json()["asks"]


def by_type(asks: list[dict], type_: str, card_name: str | None = None) -> dict:
    return next(a for a in asks if a["type"] == type_ and (card_name is None or a["card_name"] == card_name))


async def test_build_orders_the_queue_and_is_idempotent(stack):
    s = await stack(STACK)
    ids = await add_cards(s)
    r = await call(s, "POST", "/build", json={"answers": ANSWERS, "schedule": MONTHLY})
    body = r.json()
    asks = body["asks"]
    assert body["built"] is True
    assert [(a["type"], a["card_name"]) for a in asks] == [
        ("late_fee_refund", "Discover it"),                 # there was a late fee: the simplest request goes first
        ("apr_reduction", "Capital One Quicksilver"),       # then the rate on the largest balance
        ("apr_reduction", "Chase Freedom Unlimited"),       # then the other cards by balance
        ("apr_reduction", "Discover it"),
        ("annual_fee", "Chase Freedom Unlimited"),          # the annual fee they mentioned
        ("due_date_move", "Capital One Quicksilver"),       # one card whose due day is far from payday
    ]
    assert [a["order"] for a in asks] == [1, 2, 3, 4, 5, 6] and all(a["status"] == "queued" and a["result"] is None for a in asks)
    assert set(asks[0]) == {"id", "type", "card_id", "card_name", "issuer", "title", "subtitle", "value_cents", "value_label", "status", "order", "result"}
    late, cap, chase, disc, fee, move = asks
    assert (late["value_cents"], late["value_label"], late["issuer"], late["card_id"]) == (2900, "$29", "discover", ids["Discover it"])
    assert late["subtitle"] == "$29 late fee on your October statement"
    assert (cap["value_cents"], cap["value_label"]) == (5200, "Each point off is worth $52 a year on this balance")
    assert cap["subtitle"] == "$5,200 at 27.99%" and cap["title"] == "Ask for a lower rate on Capital One Quicksilver"
    assert (chase["value_cents"], disc["value_cents"]) == (3400, 1900)
    assert fee["value_cents"] is None and fee["value_label"] == "The annual fee on this card"
    assert move["value_cents"] is None and move["value_label"]
    assert move["subtitle"] == "Due on the 9th, 24 days after payday. Ask for the 18th."
    assert "hardship" not in {a["type"] for a in asks}  # never queued automatically
    assert body["sprint"] == {"done": 0, "total": 6, "ends_on": (DAY + dt.timedelta(days=14)).isoformat()}

    again = (await call(s, "POST", "/build", json={"answers": {**ANSWERS, "annual_fee_card": "Discover it"}})).json()
    assert again["built"] is False and [a["id"] for a in again["asks"]] == [a["id"] for a in asks]
    listed = (await call(s, "GET", "")).json()
    assert listed == {"asks": asks, "sprint": body["sprint"]}
    assert (await call(s, "GET", "", user="u2")).json() == {"asks": [], "sprint": None}


async def test_build_without_extras_asks_only_for_rates(stack):
    s = await stack(STACK)
    await add_cards(s, cards=(CHASE, {**DISC, "balance_cents": 0}))
    asks = await built(s, answers={}, schedule={"frequency": "biweekly", "anchor": "2026-11-20", "per_payday_cents": 30_000})
    assert [(a["type"], a["card_name"]) for a in asks] == [("apr_reduction", "Chase Freedom Unlimited")]  # nothing owed on the other card
    # nobody with no cards gets a sprint; the queue is built once the cards exist
    empty = (await call(s, "POST", "/build", user="u9", json={})).json()
    assert empty == {"asks": [], "sprint": None, "built": False}
    await add_cards(s, user="u9", cards=(CAP,))
    assert (await call(s, "POST", "/build", user="u9", json={})).json()["built"] is True
    # a due day in the week after payday is left alone
    await add_cards(s, user="u8", cards=({**CAP, "due_day": 18},))
    assert [a["type"] for a in await built(s, user="u8", answers={})] == ["apr_reduction"]


async def test_queued_rate_asks_follow_the_balance(stack):
    s = await stack(STACK)
    ids = await add_cards(s, cards=(CAP,))
    [ask] = await built(s, answers={}, schedule=None)
    await s.internal("POST", f"/internal/cards/{ids[CAP['name']]}/events", user="u1", clock_offset_s=NOW,
                     json={"type": "payment", "amount_cents": 20_000, "ref": "p1"})
    fresh = (await call(s, "GET", "")).json()["asks"][0]
    assert fresh["value_cents"] == 5000 and fresh["subtitle"] == "$5,000 at 27.99%" and fresh["id"] == ask["id"]
    assert (await call(s, "GET", f"/{ask['id']}")).json()["script"]["lines"][1]["text"].startswith("I'm carrying a balance of $5,000 at 27.99%.")


async def test_cards_imported_event_builds_the_queue(stack):
    s = await stack(STACK)
    await add_cards(s)
    await s.drain()
    async with session("asks") as db:
        await bus.emit(db, "asks", "cards.imported", {"user_id": "u1", "answers": ANSWERS, "schedule": MONTHLY})
    assert await s.drain() >= 1
    asks = (await call(s, "GET", "")).json()["asks"]
    assert [a["type"] for a in asks] == ["late_fee_refund", "apr_reduction", "apr_reduction", "apr_reduction", "annual_fee", "due_date_move"]
    async with session("asks") as db:  # delivered again: nothing is added
        await bus.emit(db, "asks", "cards.imported", {"user_id": "u1", "answers": ANSWERS})
    await s.drain()
    assert len((await call(s, "GET", "")).json()["asks"]) == 6


async def test_statement_triggers_add_asks_once(stack):
    s = await stack(STACK)
    ids = await add_cards(s)
    cap = ids[CAP["name"]]
    await built(s, answers={}, schedule=None)  # three rate asks
    await s.drain()

    async def reconciled(statement_id: str, triggers: list[dict], card_id: str = cap) -> None:
        async with session("asks") as db:
            await bus.emit(db, "asks", "statement.reconciled", {"user_id": "u1", "card_id": card_id, "statement_id": statement_id,
                                                                "close_date": "2026-12-11", "triggers": triggers})
        await s.drain()

    late = {"type": "late_fee_refund", "title": "A $29 late fee was charged", "detail": "It is on your December statement.", "value_cents": 2900}
    rate = {"type": "apr_reduction", "title": "Your rate went up", "detail": "27.99% to 28.24% on this statement."}
    await reconciled("st1", [late, rate, {"type": "something_else", "title": "ignored"}])
    asks = (await call(s, "GET", "")).json()["asks"]
    assert len(asks) == 4  # the late fee is new; a rate ask for this card is still queued, so no second one
    first = asks[0]
    assert (first["type"], first["card_id"], first["title"], first["subtitle"], first["value_cents"], first["value_label"]) == (
        "late_fee_refund", cap, "A $29 late fee was charged", "It is on your December statement.", 2900, "$29")
    detail = (await call(s, "GET", f"/{first['id']}")).json()
    assert "I was charged a $29 late fee on my December statement." in detail["script"]["lines"][1]["text"]

    await reconciled("st1", [late, rate])  # the same statement again
    assert len((await call(s, "GET", "")).json()["asks"]) == 4
    await reconciled("st2", [late])  # another statement, but the first late-fee ask is still queued
    assert len((await call(s, "GET", "")).json()["asks"]) == 4
    await call(s, "POST", f"/{first['id']}/outcome", json={"result": "refused", "channel": "phone"})
    await reconciled("st2", [late])  # the same statement cannot produce it twice...
    await reconciled("st3", [late, {"type": "hardship", "title": "Minimums are above your budget", "detail": "Only if you cannot cover them."}])
    asks = (await call(s, "GET", "")).json()["asks"]
    assert [a["type"] for a in asks if a["status"] == "queued"].count("late_fee_refund") == 1  # ...and st2 already did, so this one is from st3
    assert by_type(asks, "hardship")["title"] == "Minimums are above your budget"
    await reconciled("st4", [late], card_id="no-such-card")  # a removed card is ignored
    assert len((await call(s, "GET", "")).json()["asks"]) == len(asks)


def assert_detail_is_well_formed(detail: dict, allowed: set[str]) -> None:
    from burnrate.services.asks import detail_texts

    assert set(detail) >= {"id", "type", "card_id", "card_name", "issuer", "title", "subtitle", "value_cents", "value_label", "status",
                           "order", "result", "why", "evidence", "script", "written", "call_card", "playbook"}
    assert set(detail["evidence"]) == {"text", "source", "caveat"} and all(detail["evidence"].values())
    script = detail["script"]
    assert set(script) == {"opening", "lines", "must_ask", "if_refused"}
    assert all(set(line) == {"label", "text"} for line in script["lines"]) and len(script["lines"]) >= 4
    assert "Will this request result in a hard inquiry?" in script["must_ask"]
    assert "when" in script["if_refused"] and "ask" in script["if_refused"]
    for question in script["must_ask"]:
        assert question in detail["written"]
    # every string a person reads or says passes the wording rules, and uses only the person's and the survey's numbers
    for text in detail_texts(detail):
        result = check_text(text, allowed_numbers=allowed)
        assert result.ok, (text, result.violations)
    # the call card is a tree of taps: every link leads somewhere, every end names an outcome, every node is reachable
    card = detail["call_card"]
    nodes = card["nodes"]
    assert card["start"] in nodes
    seen, todo = set(), [card["start"]]
    while todo:
        node_id = todo.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        node = nodes[node_id]
        assert isinstance(node["say"], str) and isinstance(node["options"], list)
        for option in node["options"]:
            assert set(option) == {"label", "next"} and option["next"] in nodes
            todo.append(option["next"])
        if not node["options"]:
            assert node["outcome"] in ("granted", "partial", "temporary", "refused", "no_answer")
        else:
            assert "outcome" not in node
    assert seen == set(nodes)
    labels = " | ".join(o["label"] for o in nodes["ask"]["options"])
    assert "said yes" in labels and "temporary" in labels and "said no" in labels and "transferring me to another department" in labels
    assert {nodes[n]["outcome"] for n in nodes if not nodes[n]["options"]} >= {"refused", "no_answer"}
    assert "qualify" in " ".join(o["label"] for o in nodes["no"]["options"]) and nodes["no"]["say"] == script["if_refused"]


async def test_details_use_the_persons_numbers_and_pass_the_wording_rules(stack):
    s = await stack(STACK)
    ids = await add_cards(s)
    asks = await built(s)
    hardship = (await call(s, "POST", "/suggest", json={"card_id": ids[DISC["name"]], "type": "hardship", "ref": "plan:short"})).json()["ask"]
    assert {a["type"] for a in asks} | {"hardship"} == {"late_fee_refund", "apr_reduction", "annual_fee", "due_date_move", "hardship"}

    rate = (await call(s, "GET", f"/{by_type(asks, 'apr_reduction', CAP['name'])['id']}")).json()
    assert_detail_is_well_formed(rate, SURVEY_NUMBERS | {"$5,200", "27.99%", "$52", "$1,455.48"})
    assert rate["script"]["opening"] == "Hi, my name is [your name]. I'm calling about my Capital One Quicksilver card ending in 4821."
    assert rate["script"]["lines"][1] == {"label": "The request", "text": "I'm carrying a balance of $5,200 at 27.99%. I'd like to ask you to lower my purchase APR."}
    assert rate["script"]["must_ask"] == ["Will this request result in a hard inquiry?", "Is the new rate permanent or temporary, and until what date?",
                                          "Which balances does it apply to?"]
    assert "largest balance" in rate["why"] and "$52 a year" in rate["why"]
    assert "84%" in rate["evidence"]["text"] and "LendingTree" in rate["evidence"]["source"] and "2026" in rate["evidence"]["source"]
    assert "chose to ask" in rate["evidence"]["caveat"] and "not a forecast" in rate["evidence"]["caveat"] and "may say no" in rate["evidence"]["caveat"]
    assert rate["written"].startswith("Hello. I'm writing about my Capital One Quicksilver card ending in 4821.")
    assert rate["playbook"] == {"count": 0, "entries": [], "note": "Self-reported by Burn Rate users; too few reports to show shares."}
    assert rate["facts"]["balance_cents"] == 520_000 and rate["facts"]["apr_bps"] == 2799 and rate["facts"]["per_point_cents"] == 5200
    assert rate["call_card"]["nodes"]["accepted_offer"]["outcome"] == "temporary"

    other = (await call(s, "GET", f"/{by_type(asks, 'apr_reduction', CHASE['name'])['id']}")).json()
    assert_detail_is_well_formed(other, SURVEY_NUMBERS | {"$3,400", "22.49%", "$34", "$764.66"})
    assert "largest" not in other["why"]

    late = (await call(s, "GET", f"/{by_type(asks, 'late_fee_refund')['id']}")).json()
    assert_detail_is_well_formed(late, SURVEY_NUMBERS | {"$29"})
    assert late["script"]["lines"][1]["text"] == "I was charged a $29 late fee on my October statement. I'd like to ask you to refund it."
    assert "automatic payments" in late["script"]["lines"][2]["text"]  # they told us autopay is on, so the line is true for them
    assert late["script"]["must_ask"] == ["Will this request result in a hard inquiry?", "Will the refund show on my next statement?"]
    assert "92%" in late["evidence"]["text"] and "no late fee the first time" in late["evidence"]["text"]  # a Discover card
    assert late["call_card"]["nodes"]["accepted_offer"]["outcome"] == "partial"

    fee = (await call(s, "GET", f"/{by_type(asks, 'annual_fee')['id']}")).json()
    assert_detail_is_well_formed(fee, SURVEY_NUMBERS)
    assert "94%" in fee["evidence"]["text"] and "statement credit, points, or a spending requirement" in " ".join(fee["script"]["must_ask"])

    move = (await call(s, "GET", f"/{by_type(asks, 'due_date_move')['id']}")).json()
    assert_detail_is_well_formed(move, set())
    assert move["script"]["lines"][1]["text"] == "My payment is due on the 9th of each month. I'd like to move the due date to the 18th."
    assert "I get paid on the 15th" in move["script"]["lines"][2]["text"] and "Regulation Z" in move["evidence"]["source"]
    assert "Which billing cycle does the new due date start with?" in move["script"]["must_ask"]

    hard = (await call(s, "GET", f"/{hardship['id']}")).json()
    assert_detail_is_well_formed(hard, set())
    four = ["What will the rate be, and for how many months?", "Will the card be frozen or closed, and will it reopen afterwards?",
            "How will the account be reported to the credit bureaus?", "What happens to the rate and the minimum payment if I miss a payment?"]
    assert all(q in hard["script"]["must_ask"] for q in four)
    nodes = hard["call_card"]["nodes"]
    walk, node = [], nodes[nodes["ask"]["options"][0]["next"]]
    while len(node["options"]) == 1:  # the four questions come one after another before any acceptance
        walk.append(node["say"])
        node = nodes[node["options"][0]["next"]]
    assert walk == four and "in writing" in node["say"]
    assert [nodes[o["next"]]["outcome"] for o in node["options"]] == ["temporary", "refused"]

    assert (await call(s, "GET", f"/{rate['id']}", user="u2")).status_code == 404
    assert (await call(s, "GET", "/nope")).json()["error"]["code"] == "ask_not_found"


async def test_rate_outcome_changes_the_card_once(stack):
    s = await stack(STACK)
    ids = await add_cards(s, cards=(CHASE,))
    chase = ids[CHASE["name"]]
    [ask] = await built(s, answers={}, schedule=None)
    await s.drain()
    seen: list[dict] = []

    @bus.subscribe("ask.outcome.logged", worker="test-listener", service="asks")
    async def listener(ev):
        seen.append(ev.payload)

    try:
        outcome = {"result": "granted", "channel": "phone", "new_apr_bps": 2049, "rep_name": "Dana", "hard_pull": "no", "note": "Asked nicely"}
        not_lower = await call(s, "POST", f"/{ask['id']}/outcome", json={**outcome, "new_apr_bps": 2249})
        assert not_lower.status_code == 422 and not_lower.json()["error"]["code"] == "not_a_reduction"
        assert (await call(s, "POST", f"/{ask['id']}/outcome", user="u2", json=outcome)).status_code == 404
        r = await call(s, "POST", f"/{ask['id']}/outcome", json=outcome)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["duplicate"] is False
        assert (body["ask"]["status"], body["ask"]["result"]) == ("done", "Rate lowered from 22.49% to 20.49%")
        assert body["outcome"] == {"id": body["outcome"]["id"], "ask_id": ask["id"], "result": "granted", "channel": "phone", "new_apr_bps": 2049,
                                   "old_apr_bps": 2249, "until": None, "refund_cents": None, "rep_name": "Dana", "hard_pull": "no",
                                   "ask_again_on": None, "note": "Asked nicely", "date": DAY.isoformat()}
        twice = (await call(s, "POST", f"/{ask['id']}/outcome", json={**outcome, "new_apr_bps": 1849})).json()
        assert twice["duplicate"] is True and twice["outcome"]["new_apr_bps"] == 2049
        await s.drain()
        card = (await s.internal("GET", "/internal/cards", user="u1", clock_offset_s=NOW)).json()["cards"][0]
        assert card["apr_bps"] == 2049
        ledger = (await s.internal("GET", f"/internal/cards/{chase}/ledger", user="u1", clock_offset_s=NOW)).json()["events"]
        changes = [e for e in ledger if e["type"] == "rate_change"]
        assert [(e["apr_bps"], e["source"], e["ref"], e["date"]) for e in changes] == [(2049, "ask", ask["id"], DAY.isoformat())]
        assert len(seen) == 1
        assert {k: seen[0][k] for k in ("issuer", "type", "channel", "result", "ask_id", "card_id", "new_apr_bps", "old_apr_bps", "demo")} == {
            "issuer": "chase", "type": "apr_reduction", "channel": "phone", "result": "granted", "ask_id": ask["id"], "card_id": chase,
            "new_apr_bps": 2049, "old_apr_bps": 2249, "demo": False}
        listed = (await call(s, "GET", "")).json()
        assert listed["sprint"]["done"] == 1 and listed["asks"][0]["status"] == "done"
        done_skip = await call(s, "POST", f"/{ask['id']}/skip")
        assert done_skip.status_code == 409 and done_skip.json()["error"]["code"] == "ask_already_done"
    finally:
        bus._subs[:] = [x for x in bus._subs if x.worker != "test-listener"]


async def test_an_interrupted_outcome_is_finished_by_calling_again(stack, monkeypatch):
    from burnrate.common import clients
    from burnrate.common.errors import Unavailable

    s = await stack(STACK)
    ids = await add_cards(s, cards=(CHASE,))
    [ask] = await built(s, answers={}, schedule=None)
    real_post, calls = clients.post, []

    async def flaky(service, path, json=None, **kw):
        calls.append(path)
        if len(calls) == 1:
            await real_post(service, path, json, **kw)  # the rate did change...
            raise Unavailable("cards timed out", code="cards_unavailable")  # ...but we never heard back
        return await real_post(service, path, json, **kw)

    monkeypatch.setattr(clients, "post", flaky)
    outcome = {"result": "temporary", "channel": "chat", "new_apr_bps": 1999, "until": "2027-05-15"}
    first = await call(s, "POST", f"/{ask['id']}/outcome", json=outcome)
    assert first.status_code == 503
    assert (await call(s, "GET", "")).json()["asks"][0]["status"] == "queued"
    second = (await call(s, "POST", f"/{ask['id']}/outcome", json=outcome)).json()
    assert second["duplicate"] is False and second["outcome"]["old_apr_bps"] == 2249  # the rate before the first attempt
    assert second["ask"]["result"] == "Rate lowered from 22.49% to 19.99% until May 15"
    assert (await s.internal("GET", "/internal/cards", user="u1", clock_offset_s=NOW)).json()["cards"][0]["apr_bps"] == 1999
    assert ids[CHASE["name"]] in calls[0]


async def test_refunds_refusals_and_unanswered_calls(stack):
    s = await stack(STACK)
    ids = await add_cards(s)
    asks = await built(s)
    late, cap_rate, fee = by_type(asks, "late_fee_refund"), by_type(asks, "apr_reduction", CAP["name"]), by_type(asks, "annual_fee")
    disc = ids[DISC["name"]]

    for _ in range(2):  # logged twice, credited once
        r = (await call(s, "POST", f"/{late['id']}/outcome", json={"result": "granted", "channel": "chat", "refund_cents": 2900})).json()
    assert r["duplicate"] is True and r["ask"]["result"] == "Late fee refunded: $29" and r["ask"]["status"] == "done"
    ledger = (await s.internal("GET", f"/internal/cards/{disc}/ledger", user="u1", clock_offset_s=NOW)).json()["events"]
    assert [(e["amount_cents"], e["source"], e["ref"]) for e in ledger if e["type"] == "credit"] == [(2900, "ask", late["id"])]
    cards = {c["id"]: c for c in (await s.internal("GET", "/internal/cards", user="u1", clock_offset_s=NOW)).json()["cards"]}
    assert cards[disc]["balance_cents"] == 190_000 - 2900

    refused = (await call(s, "POST", f"/{cap_rate['id']}/outcome",
                          json={"result": "refused", "channel": "phone", "ask_again_on": "2027-01-15", "new_apr_bps": 1999})).json()
    assert (refused["ask"]["status"], refused["ask"]["result"]) == ("done", "Refused. Ask again on Jan 15.")
    assert refused["outcome"]["new_apr_bps"] is None and cards[ids[CAP["name"]]]["apr_bps"] == 2799  # a refusal changes nothing

    quiet = (await call(s, "POST", f"/{fee['id']}/outcome", json={"result": "no_answer", "channel": "phone"})).json()
    assert (quiet["ask"]["status"], quiet["ask"]["result"]) == ("queued", "No answer. Try again at another time of day.")
    assert (await call(s, "POST", f"/{fee['id']}/outcome", json={"result": "no_answer", "channel": "phone"})).json()["duplicate"] is True
    later = (await call(s, "POST", f"/{fee['id']}/outcome", json={"result": "partial", "channel": "phone", "refund_cents": 4750})).json()
    assert (later["duplicate"], later["ask"]["status"], later["ask"]["result"]) == (False, "done", "$47.50 refunded")

    skipped = (await call(s, "POST", f"/{by_type(asks, 'due_date_move')['id']}/skip")).json()
    assert skipped["status"] == "skipped"
    sprint = (await call(s, "GET", "")).json()["sprint"]
    assert (sprint["done"], sprint["total"]) == (3, 5)  # a skipped request leaves the sprint
    for result in (r["ask"]["result"], refused["ask"]["result"], quiet["ask"]["result"], later["ask"]["result"]):
        assert check_text(result).ok


async def test_meter_counts_refunds_once_and_rate_cuts_per_day(stack):
    s = await stack(STACK)
    ids = await add_cards(s)
    asks = await built(s)
    assert (await call(s, "GET", "/meter")).json() == {"items": [], "confirmed_cents": 0}
    await call(s, "POST", f"/{by_type(asks, 'late_fee_refund')['id']}/outcome", json={"result": "granted", "channel": "chat", "refund_cents": 2900})
    await call(s, "POST", f"/{by_type(asks, 'apr_reduction', CAP['name'])['id']}/outcome",
               json={"result": "granted", "channel": "phone", "new_apr_bps": 2599})
    await call(s, "POST", f"/{by_type(asks, 'apr_reduction', CHASE['name'])['id']}/outcome", json={"result": "refused", "channel": "phone"})
    today_items = (await call(s, "GET", "/meter")).json()["items"]
    assert today_items == [
        {"label": "Late fee refunded on Discover it", "cents": 2900, "kind": "confirmed", "date": DAY.isoformat()},
        {"label": "Lower rate on Capital One Quicksilver: 27.99% to 25.99%, 0 days so far", "cents": 0, "kind": "confirmed", "date": DAY.isoformat()}]
    # ten days later, after a payment: today's balance x 2 points / 365 x 10 days
    later = offset_to(DAY + dt.timedelta(days=10))
    await s.internal("POST", f"/internal/cards/{ids[CAP['name']]}/events", user="u1", clock_offset_s=later,
                     json={"type": "payment", "amount_cents": 20_000, "ref": "p1"})
    body = (await call(s, "GET", "/meter", offset=later)).json()
    rate_item = body["items"][1]
    assert rate_item["cents"] == round(500_000 * 0.02 / 365 * 10) == 274 and "10 days so far" in rate_item["label"]
    assert body["confirmed_cents"] == 2900 + 274 and body["items"][0]["cents"] == 2900
    assert (await call(s, "GET", "/meter", user="u2")).json()["items"] == []


async def test_playbook_counts_reports_and_shows_shares_only_from_twenty(stack):
    s = await stack(STACK)
    empty = (await call(s, "GET", "/playbook", params={"issuer": "chase", "type": "apr_reduction"})).json()
    assert empty == {"count": 0, "entries": [], "note": "Self-reported by Burn Rate users; too few reports to show shares."}

    async def report(user: str, result: str, channel: str = "phone", demo: bool = False) -> None:
        await s.internal("POST", "/internal/cards", json={**CHASE, "opened_on": "2026-11-01"}, user=user, clock_offset_s=NOW, demo=demo)
        r = await s.internal("POST", "/internal/asks/build", json={}, user=user, clock_offset_s=NOW, demo=demo)
        ask_id = r.json()["asks"][0]["id"]
        body = {"result": result, "channel": channel, **({"new_apr_bps": 2049} if result == "granted" else {})}
        assert (await s.internal("POST", f"/internal/asks/{ask_id}/outcome", json=body, user=user, clock_offset_s=NOW, demo=demo)).status_code == 200

    for i in range(12):
        await report(f"a{i}", "granted")
    for i in range(5):
        await report(f"b{i}", "refused")
    await report("c0", "no_answer", channel="chat")
    await report("guest", "granted", demo=True)  # practice in a demo session is not a report about a real issuer
    await s.drain()
    few = (await call(s, "GET", "/playbook", params={"issuer": "chase", "type": "apr_reduction"})).json()
    month = DAY.isoformat()[:7]
    assert few["count"] == 18 and few["note"] == "Self-reported by Burn Rate users; too few reports to show shares."
    assert few["entries"] == [{"channel": "phone", "result": "granted", "n": 12, "month": month},
                              {"channel": "phone", "result": "refused", "n": 5, "month": month},
                              {"channel": "chat", "result": "no_answer", "n": 1, "month": month}]
    assert (await call(s, "GET", "/playbook", params={"issuer": "discover", "type": "apr_reduction"})).json()["count"] == 0
    assert (await call(s, "GET", "/playbook", params={"issuer": "chase", "type": "late_fee_refund"})).json()["count"] == 0

    await report("d0", "refused")
    await report("d1", "granted", channel="message")
    await s.drain()
    enough = (await call(s, "GET", "/playbook", params={"issuer": "chase", "type": "apr_reduction"})).json()
    assert enough["count"] == 20 and "Self-reported by Burn Rate users" in enough["note"] and "20 reports" in enough["note"]
    assert [(e["channel"], e["result"], e["n"], e["share"]) for e in enough["entries"]] == [
        ("phone", "granted", 12, 0.6), ("phone", "refused", 6, 0.3), ("chat", "no_answer", 1, 0.05), ("message", "granted", 1, 0.05)]
    assert check_text(enough["note"]).ok
    # the ask detail carries the same numbers for its issuer and request type
    ask_id = (await call(s, "GET", "", user="a0")).json()["asks"][0]["id"]
    assert (await call(s, "GET", f"/{ask_id}", user="a0")).json()["playbook"]["count"] == 20

    # an event delivered twice is counted once
    from burnrate.services import asks as svc

    ev = bus.Event(id="e-1", topic="ask.outcome.logged", key="", payload={"issuer": "citi", "type": "annual_fee", "channel": "phone",
                                                                           "result": "granted", "on": "2026-11-20"})
    await svc.on_outcome_logged(ev)
    await svc.on_outcome_logged(ev)
    assert (await call(s, "GET", "/playbook", params={"issuer": "citi", "type": "annual_fee"})).json()["count"] == 1
    stats = (await call(s, "GET", "/stats")).json()
    assert stats["playbook_reports"] == 21 and stats["by_result"] == {"granted": 14, "refused": 6, "no_answer": 1}
    assert stats["by_status"] == {"done": 20, "queued": 1} and stats["sprints"] == 21 and stats["by_type"] == {"apr_reduction": 21}


async def test_suggest_is_idempotent_by_ref(stack):
    s = await stack(STACK)
    ids = await add_cards(s, cards=(CAP, DISC))
    await built(s, answers={}, schedule=None)
    payload = {"card_id": ids[DISC["name"]], "type": "late_fee_refund", "title": "A late fee showed up", "detail": "On the December statement.",
               "value_cents": 4100, "ref": "reconcile:st9"}
    first = (await call(s, "POST", "/suggest", json=payload)).json()
    assert first["created"] is True
    assert (first["ask"]["title"], first["ask"]["value_cents"], first["ask"]["value_label"], first["ask"]["order"]) == ("A late fee showed up", 4100, "$41", 1)
    second = (await call(s, "POST", "/suggest", json=payload)).json()
    assert second["created"] is False and second["ask"]["id"] == first["ask"]["id"]
    assert len((await call(s, "GET", "")).json()["asks"]) == 3
    rate = (await call(s, "POST", "/suggest", json={"card_id": ids[CAP["name"]], "type": "apr_reduction", "ref": "x"})).json()
    assert rate["created"] is False  # the same request on the same card is already waiting
    assert (await call(s, "POST", "/suggest", json={**payload, "type": "credit_limit", "ref": "y"})).json()["error"]["code"] == "bad_ask_type"
    assert (await call(s, "POST", "/suggest", json={**payload, "card_id": "nope", "ref": "z"})).json()["error"]["code"] == "card_not_found"


async def test_seed_inserts_history_without_touching_cards_or_the_playbook(stack):
    s = await stack(STACK)
    ids = await add_cards(s)
    await s.drain()
    chase, cap = ids[CHASE["name"]], ids[CAP["name"]]
    seed = {
        "sprint_started_on": "2026-11-06",
        "asks": [
            {"id": "ask-late", "type": "late_fee_refund", "card_id": ids[DISC["name"]], "card_name": "Discover it", "issuer": "discover",
             "status": "done", "created_on": "2026-11-06", "done_on": "2026-11-07", "facts": {"fee_cents": 2900, "fee_month": "October"}},
            {"id": "ask-chase", "type": "apr_reduction", "card_id": chase, "card_name": "Chase Freedom Unlimited", "issuer": "chase",
             "status": "done", "created_on": "2026-11-06", "done_on": "2026-11-10"},
            {"id": "ask-cap", "type": "apr_reduction", "card_id": cap, "card_name": "Capital One Quicksilver", "issuer": "capital_one",
             "status": "queued", "created_on": "2026-11-06"},
        ],
        "outcomes": [
            {"ask_id": "ask-late", "result": "granted", "channel": "chat", "refund_cents": 2900, "date": "2026-11-07"},
            {"ask_id": "ask-chase", "result": "granted", "channel": "phone", "old_apr_bps": 2449, "new_apr_bps": 2249, "date": "2026-11-10",
             "rep_name": "Dana", "hard_pull": "no"},
        ],
    }
    r = await call(s, "POST", "/seed", json=seed)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == {"asks": 3, "outcomes": 2}
    assert [(a["id"], a["status"], a["result"]) for a in body["asks"]] == [
        ("ask-late", "done", "Late fee refunded: $29"), ("ask-chase", "done", "Rate lowered from 24.49% to 22.49%"), ("ask-cap", "queued", None)]
    assert body["sprint"] == {"done": 2, "total": 3, "ends_on": "2026-11-20"}
    assert body["asks"][2]["value_label"] == "Each point off is worth $52 a year on this balance"
    assert (await call(s, "POST", "/seed", json=seed)).json()["added"] == {"asks": 0, "outcomes": 0}
    # no side effects: the ledger has only the opening balances, nothing waits in the outbox, the playbook is empty
    for card_id in ids.values():
        events = (await s.internal("GET", f"/internal/cards/{card_id}/ledger", user="u1", clock_offset_s=NOW)).json()["events"]
        assert [e["type"] for e in events] == ["opening"]
    assert await s.drain() == 0
    assert (await call(s, "GET", "/playbook")).json()["count"] == 0
    # the seeded history feeds the meter: 10 days of 2 points on Chase's current balance, and the refund
    meter = (await call(s, "GET", "/meter")).json()
    assert [i["cents"] for i in meter["items"]] == [2900, round(340_000 * 0.02 / 365 * 10)]
    detail = (await call(s, "GET", "/ask-cap")).json()
    assert detail["script"]["lines"][1]["text"].startswith("I'm carrying a balance of $5,200 at 27.99%.")
    bad = await call(s, "POST", "/seed", json={"outcomes": [{"ask_id": "missing", "result": "refused"}]})
    assert bad.status_code == 404


async def test_seed_by_key_gives_every_demo_guest_their_own_rows(stack):
    """The demo service seeds the same persona for every guest, by `key` and `ask_key`, without card names."""
    s = await stack(STACK)
    seen: dict[str, list[str]] = {}
    for guest in ("g1", "g2"):
        ids = await add_cards(s, user=guest)
        payload = {
            "sprint_started_on": "2026-11-06",
            "asks": [
                {"key": "chase-rate", "card_id": ids[CHASE["name"]], "type": "apr_reduction", "status": "done", "order": 0,
                 "created_on": "2026-11-06", "done_on": "2026-11-10", "result": "Rate lowered from 24.49% to 22.49%"},
                {"key": "chase-fee", "card_id": ids[CHASE["name"]], "type": "late_fee_refund", "status": "queued", "order": 1,
                 "created_on": "2026-11-06", "value_cents": 3_900},
                {"key": "cap-rate", "card_id": ids[CAP["name"]], "type": "apr_reduction", "status": "queued", "order": 2, "created_on": "2026-11-06"},
            ],
            "outcomes": [{"ask_key": "chase-rate", "result": "granted", "channel": "phone", "old_apr_bps": 2449, "new_apr_bps": 2249,
                          "date": "2026-11-10", "hard_pull": "no"}],
        }
        r = await s.internal("POST", "/internal/asks/seed", json=payload, user=guest, demo=True, clock_offset_s=NOW)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["added"] == {"asks": 3, "outcomes": 1}
        assert [(a["type"], a["card_name"], a["issuer"], a["status"], a["order"]) for a in body["asks"]] == [
            ("apr_reduction", "Chase Freedom Unlimited", "chase", "done", 1), ("late_fee_refund", "Chase Freedom Unlimited", "chase", "queued", 2),
            ("apr_reduction", "Capital One Quicksilver", "capital_one", "queued", 3)]
        fee = body["asks"][1]
        assert (fee["value_cents"], fee["value_label"], fee["subtitle"]) == (3900, "$39", "$39 late fee")
        detail = (await s.internal("GET", f"/internal/asks/{fee['id']}", user=guest, demo=True, clock_offset_s=NOW)).json()
        assert "I was charged a $39 late fee on my [month] statement." in detail["script"]["lines"][1]["text"]
        again = await s.internal("POST", "/internal/asks/seed", json=payload, user=guest, demo=True, clock_offset_s=NOW)
        assert again.json()["added"] == {"asks": 0, "outcomes": 0}
        meter = (await s.internal("GET", "/internal/asks/meter", user=guest, demo=True, clock_offset_s=NOW)).json()
        assert [i["cents"] for i in meter["items"]] == [round(340_000 * 0.02 / 365 * 10)]
        seen[guest] = [a["id"] for a in body["asks"]]
    assert not set(seen["g1"]) & set(seen["g2"])  # the same keys, different rows
    taken = await s.internal("POST", "/internal/asks/seed", user="g2", clock_offset_s=NOW,
                             json={"asks": [{"id": seen["g1"][0], "card_id": "x", "type": "hardship"}]})
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "ask_id_taken"
    nameless = await s.internal("POST", "/internal/asks/seed", user="g2", clock_offset_s=NOW, json={"asks": [{"card_id": "x", "type": "hardship"}]})
    assert nameless.status_code == 422


async def test_an_outcome_is_not_stored_while_the_cards_service_is_away(stack, monkeypatch):
    from burnrate.common import clients
    from burnrate.common.errors import Unavailable

    s = await stack(STACK)
    await add_cards(s, cards=(CHASE,))
    [ask] = await built(s, answers={}, schedule=None)
    real_get = clients.get

    async def down(service, path, **kw):
        if service == "cards":
            raise Unavailable("cards is not reachable", code="cards_unavailable")
        return await real_get(service, path, **kw)

    monkeypatch.setattr(clients, "get", down)
    r = await call(s, "POST", f"/{ask['id']}/outcome", json={"result": "granted", "channel": "phone", "new_apr_bps": 2049})
    assert r.status_code == 503 and r.json()["error"]["code"] == "cards_unavailable"
    listed = (await call(s, "GET", "")).json()  # the list still renders, from the numbers stored with the ask
    assert listed["asks"][0]["status"] == "queued" and listed["asks"][0]["subtitle"] == "$3,400 at 22.49%"
    monkeypatch.setattr(clients, "get", real_get)
    ok = (await call(s, "POST", f"/{ask['id']}/outcome", json={"result": "granted", "channel": "phone", "new_apr_bps": 2049})).json()
    assert ok["duplicate"] is False and ok["ask"]["result"] == "Rate lowered from 22.49% to 20.49%"
