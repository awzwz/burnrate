"""llm-gateway on the fake provider. Nothing in this file can reach a paid API: the stack runs with BR_ENV=test
(the gateway then always answers with the fake), and the two SDK round-trip tests use an in-process mock transport."""
import base64
import datetime as dt
import inspect
import json

import pytest

from burnrate.common.config import Settings
from burnrate.common.context import real_now
from burnrate.common.db import new_id, session
from burnrate.common.llm import anthropic_adapter, openai_adapter
from burnrate.common.llm.base import (
    Document,
    LLMBadOutput,
    LLMError,
    LLMRefusal,
    Usage,
    statement_schema,
    to_statement_fields,
)
from burnrate.common.llm.fake import parse_statement_text, scripted_reply
from burnrate.common.llm.pricing import cost_usd, price_for

STATEMENT = """
CAPITAL ONE QUICKSILVER                         Account ending in 4821
Statement Closing Date: 12/11/2026              Days in Billing Cycle: 30

Previous Balance                    $5,200.00
Payments                             -$240.00
Purchases                             $120.00
Fees Charged                           $29.00
Interest Charged                      $119.33
New Balance                         $5,228.33
Minimum Payment Due                   $171.00
Payment Due Date                    January 9, 2027

Late Payment Warning: If we do not receive your minimum payment by the date listed above,
you may have to pay a late fee of up to $41.00.
Late Fee                               $29.00

Interest Charge Calculation
Annual Percentage Rate (APR): 28.24% (v)
Balance Subject to Interest Rate    $5,140.25
Total Interest Charged in 2026      $1,203.44
"""
EXPECTED = {"new_balance_cents": 522_833, "previous_balance_cents": 520_000, "payments_credits_cents": 24_000,
            "purchases_cents": 12_000, "fees_cents": 2_900, "interest_cents": 11_933, "apr_bps": 2824,
            "balance_subject_to_interest_cents": 514_025, "days_in_cycle": 30, "min_payment_cents": 17_100,
            "close_date": "2026-12-11", "due_date": "2027-01-09", "late_fee_cents": 2_900, "interest_ytd_cents": 120_344}
LLM = ["llm-gateway"]


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    """Belt and braces: even if a .env file ever holds real keys, these tests do not see them."""
    monkeypatch.setenv("BR_OPENAI_API_KEY", "")
    monkeypatch.setenv("BR_ANTHROPIC_API_KEY", "")


async def spend(user: str, usd: float, *, day: dt.date | None = None, demo: bool = False) -> None:
    from burnrate.services import llm_gateway as gw

    async with session(gw.SVC) as s:
        s.add(gw.UsageRow(id=new_id(), day=day or real_now().date(), user_id=user, demo=demo, task="test", provider="openai",
                          model="gpt-5.4-mini", input_tokens=1, output_tokens=1, cost_usd=usd, ok=True, created_at=real_now()))


# ---------- statements ----------

async def test_fake_reads_a_text_statement_into_cents_and_basis_points(stack):
    s = await stack(LLM)
    r = await s.internal("POST", "/internal/llm/extract-statement", user="u1",
                         json={"filename": "dec.txt", "mime": "text/plain", "text": STATEMENT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fields"] == EXPECTED
    assert (body["provider"], body["model"], body["cost_usd"], body["dropped"]) == ("fake", "fake-rep-1", 0.0, [])
    as_file = await s.internal("POST", "/internal/llm/extract-statement", user="u1",
                               json={"filename": "dec.txt", "mime": "text/plain", "data_b64": base64.b64encode(STATEMENT.encode()).decode()})
    assert as_file.json()["fields"] == EXPECTED


async def test_fake_leaves_out_what_the_statement_does_not_print(stack):
    s = await stack(LLM)
    r = await s.internal("POST", "/internal/llm/extract-statement", user="u1",
                         json={"filename": "short.txt", "mime": "text/plain", "text": "New Balance: 1,000.00\nClosing Date 2026-11-30\n"})
    assert r.json()["fields"] == {"new_balance_cents": 100_000, "close_date": "2026-11-30"}


async def test_fake_answers_a_pdf_or_photo_with_a_fixed_consistent_sample(stack):
    s = await stack(LLM)
    pdf = base64.b64encode(b"%PDF-1.7 not really a statement").decode()
    r = await s.internal("POST", "/internal/llm/extract-statement", user="u1",
                         json={"filename": "dec.pdf", "mime": "application/pdf", "data_b64": pdf})
    body = r.json()
    f = body["fields"]
    assert body["provider"] == "fake" and f["apr_bps"] == 2799 and f["days_in_cycle"] == 30
    assert f["new_balance_cents"] == f["previous_balance_cents"] - f["payments_credits_cents"] + f["purchases_cents"] + f["fees_cents"] + f["interest_cents"]
    assert f["interest_cents"] == round(f["balance_subject_to_interest_cents"] * 0.2799 * 30 / 365)
    assert dt.date.fromisoformat(f["due_date"]) - dt.date.fromisoformat(f["close_date"]) == dt.timedelta(days=25)
    jpg = await s.internal("POST", "/internal/llm/extract-statement", user="u1",
                           json={"filename": "dec.jpg", "mime": "image/jpg", "data_b64": pdf})
    assert jpg.json()["fields"] == f


async def test_extraction_refuses_unusable_input(stack):
    s = await stack(LLM)
    cases = [({"filename": "x", "mime": "text/plain"}, 422, "nothing_to_read"),
             ({"filename": "x.pdf", "mime": "application/pdf", "data_b64": "not base64!!"}, 422, "bad_file"),
             ({"filename": "x.docx", "mime": "application/msword", "data_b64": "QUJD"}, 422, "unsupported_file")]
    for payload, status, code in cases:
        r = await s.internal("POST", "/internal/llm/extract-statement", json=payload, user="u1")
        assert (r.status_code, r.json()["error"]["code"]) == (status, code)


async def test_model_output_that_is_not_statement_fields_is_a_domain_error(stack, monkeypatch):
    from burnrate.common.llm import fake
    from burnrate.common.llm.base import ExtractResult

    s = await stack(LLM)
    answers = iter([["not", "an", "object"], {"new_balance": {"nested": 1}}, {"new_balance": "lots", "apr_percent": "2824"},
                    {"new_balance": "5319.33", "apr_percent": "2824", "close_date": "soon", "days_in_cycle": 30}])

    async def garbage(self, doc):
        return ExtractResult(raw=next(answers), usage=Usage(10, 10))

    monkeypatch.setattr(fake.FakeProvider, "extract", garbage)
    for _ in range(3):
        r = await s.internal("POST", "/internal/llm/extract-statement", user="u1", json={"filename": "a.txt", "mime": "text/plain", "text": "x"})
        assert r.status_code == 502 and r.json()["error"]["code"] == "llm_bad_output"
    partly = (await s.internal("POST", "/internal/llm/extract-statement", user="u1", json={"filename": "a.txt", "mime": "text/plain", "text": "x"})).json()
    assert partly["fields"] == {"new_balance_cents": 531_933, "days_in_cycle": 30} and partly["dropped"] == ["apr_bps", "close_date"]


def test_figures_are_converted_in_code_not_by_the_model():
    fields, dropped = to_statement_fields({"new_balance": "$5,319.33", "payments_credits": "(240.00)", "apr_percent": 28.24,
                                           "due_date": "01/09/2027", "close_date": "Dec 11, 2026", "interest": None, "fees": ""})
    assert fields == {"new_balance_cents": 531_933, "payments_credits_cents": 24_000, "apr_bps": 2824,
                      "close_date": "2026-12-11", "due_date": "2027-01-09"} and dropped == []
    assert to_statement_fields({"purchases": "-12.00", "fees": "0.00"}) == ({"fees_cents": 0}, ["purchases_cents"])
    with pytest.raises(LLMBadOutput):
        to_statement_fields({})
    with pytest.raises(LLMBadOutput):
        to_statement_fields("New Balance 12")
    parsed = parse_statement_text(STATEMENT)
    assert parsed["late_fee"] == "$29.00" and parsed["interest"] == "$119.33"  # the warning line and the yearly total are not picked up


# ---------- chat ----------

async def test_chat_plays_a_bank_representative_by_keywords_and_scenario(stack):
    s = await stack(LLM)

    async def say(text: str, scenario: str = "agreeable", history: list | None = None) -> str:
        r = await s.internal("POST", "/internal/llm/chat", user="u1",
                             json={"task": "rehearsal", "system": f"You are a phone rep. Scenario: {scenario}",
                                   "messages": [*(history or []), {"role": "user", "content": text}]})
        assert r.status_code == 200, r.text
        assert r.json()["provider"] == "fake" and r.json()["cost_usd"] == 0.0
        return r.json()["text"]

    assert "this is Jordan" in await say("(The call connects. Answer the phone as the representative.)")
    assert "What can I help you with" in await say("Hello!")
    assert "lower the purchase rate by two percentage points" in await say("I'd like to ask you to lower my APR.")
    assert "three points lower for the next six billing cycles" in await say("Can you reduce my interest rate?", "temporary_offer")
    assert "not able to change the rate" in await say("Please lower my rate", "refusal")
    assert "refund it" in await say("Could you refund the late fee from November?")
    assert "permanent change" in await say("Is that permanent or temporary?")
    assert "six billing cycles" in await say("Is that permanent or temporary?", "temporary_offer")
    assert "doesn't involve a hard inquiry" in await say("Will this request result in a hard inquiry?")
    both = await say("Is it permanent, and will there be a hard inquiry?")
    assert "permanent change" in both and "hard inquiry" in both
    transfer = await say("I'd like to speak to a supervisor or the retention team.")
    assert "transfer you" in transfer
    assert "transfer you" in await say("Please lower my rate", "transfer")
    after = await say("Please lower my rate", "transfer", [{"role": "user", "content": "Please lower my rate"}, {"role": "assistant", "content": transfer}])
    assert "two percentage points" in after
    assert "You're welcome" in await say("Thank you so much.")
    assert "tell me a little more" in await say("Purple monkey dishwasher.")


def test_the_scripted_rep_never_breaks_the_wording_rules():
    from burnrate.common.guardrails import check_text

    lines = ["(The call connects.)", "hello", "lower my APR please", "refund the late fee", "is it permanent?", "which balances does it apply to?",
             "will there be a hard inquiry?", "supervisor please", "thank you", "move my due date", "I can't afford the minimum", "what would qualify me?",
             "yes, go ahead", "will the refund show on my next statement?", "blah"]
    for scenario in ("agreeable", "temporary_offer", "refusal", "transfer"):
        for line in lines:
            reply = scripted_reply(f"Scenario: {scenario}", [{"role": "user", "content": line}])
            assert check_text(reply).ok, (scenario, line, reply)


async def test_streamed_chat_is_ndjson_deltas_then_done(stack):
    s = await stack(LLM)
    payload = {"task": "rehearsal", "system": "Scenario: agreeable", "stream": True,
               "messages": [{"role": "user", "content": "Will this request result in a hard inquiry?"}]}
    r = await s.internal("POST", "/internal/llm/chat", json=payload, user="u1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in r.text.splitlines()]
    deltas, done = lines[:-1], lines[-1]
    assert len(deltas) > 3 and all(set(d) == {"delta"} for d in deltas)  # word by word
    assert done == {"done": True, "text": "No, this request doesn't involve a hard inquiry.", "provider": "fake",
                    "model": "fake-rep-1", "cost_usd": 0.0}
    assert "".join(d["delta"] for d in deltas) == done["text"]
    plain = await s.internal("POST", "/internal/llm/chat", json={**payload, "stream": False}, user="u1")
    assert plain.json() == {"text": done["text"], "provider": "fake", "model": "fake-rep-1", "cost_usd": 0.0}


async def test_chat_validates_the_conversation(stack):
    s = await stack(LLM)
    prefill = await s.internal("POST", "/internal/llm/chat", user="u1",
                               json={"task": "t", "system": "", "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Sure, "}]})
    assert prefill.status_code == 422 and prefill.json()["error"]["code"] == "bad_messages"
    role = await s.internal("POST", "/internal/llm/chat", user="u1", json={"task": "t", "system": "", "messages": [{"role": "system", "content": "x"}]})
    assert role.status_code == 422 and role.json()["error"]["code"] == "invalid_input"


async def test_a_provider_failure_mid_stream_ends_the_stream_with_an_error_line(stack, monkeypatch):
    from burnrate.common.llm import fake
    from burnrate.common.llm.base import ChatDelta

    s = await stack(LLM)

    async def broken(self, system, messages, max_tokens):
        yield ChatDelta("I can")
        raise LLMRefusal("The model declined this request.")

    monkeypatch.setattr(fake.FakeProvider, "chat_stream", broken)
    payload = {"task": "t", "system": "", "messages": [{"role": "user", "content": "hi"}]}
    r = await s.internal("POST", "/internal/llm/chat", json={**payload, "stream": True}, user="u1")
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert lines == [{"delta": "I can"}, {"error": {"code": "llm_refused", "message": "The model declined this request.", "status": 502}}]
    plain = await s.internal("POST", "/internal/llm/chat", json=payload, user="u1")
    assert plain.status_code == 502 and plain.json()["error"]["code"] == "llm_refused"


# ---------- budgets ----------

async def test_every_call_is_recorded_and_status_reports_the_day(stack):
    from sqlalchemy import select

    from burnrate.services import llm_gateway as gw

    s = await stack(LLM)
    await s.internal("POST", "/internal/llm/extract-statement", user="u1", json={"filename": "a.txt", "mime": "text/plain", "text": STATEMENT})
    await s.internal("POST", "/internal/llm/chat", user="u1", demo=True, clock_offset_s=40 * 86400,
                     json={"task": "rehearsal", "system": "", "messages": [{"role": "user", "content": "hello"}]})
    async with session(gw.SVC) as db:
        rows = (await db.execute(select(gw.UsageRow).order_by(gw.UsageRow.created_at))).scalars().all()
    assert [(r.task, r.user_id, r.demo, r.provider, r.model, r.cost_usd, r.ok) for r in rows] == [
        ("extract-statement", "u1", False, "fake", "fake-rep-1", 0.0, True), ("rehearsal", "u1", True, "fake", "fake-rep-1", 0.0, True)]
    assert all(r.day == real_now().date() and r.input_tokens > 0 and r.output_tokens > 0 for r in rows)  # real UTC day, not the demo clock
    await spend("u2", 0.5)
    status = (await s.internal("GET", "/internal/llm/status", user="u2")).json()
    assert status["provider"] == "fake" and status["live"] is False and status["model"] == "fake-rep-1"
    assert status["spent_today_usd"] == 0.5 and status["budget_usd"] == 3.0
    assert status["user_spent_today_usd"] == 0.5 and status["user_budget_usd"] == 0.15 and status["day"] == real_now().date().isoformat()


async def test_budget_refuses_before_the_provider_is_called(stack, monkeypatch):
    from burnrate.common.llm import fake

    s = await stack(LLM)
    calls: list[str] = []
    real_extract = fake.FakeProvider.extract

    async def counting(self, doc):
        calls.append("extract")
        return await real_extract(self, doc)

    monkeypatch.setattr(fake.FakeProvider, "extract", counting)
    extract = {"filename": "a.txt", "mime": "text/plain", "text": STATEMENT}
    chat = {"task": "rehearsal", "system": "", "messages": [{"role": "user", "content": "hello"}]}

    await spend("u1", 0.149)
    assert (await s.internal("POST", "/internal/llm/extract-statement", json=extract, user="u1")).status_code == 200
    await spend("u1", 0.001)  # now exactly at the $0.15 limit for a signed-in user
    refused = await s.internal("POST", "/internal/llm/extract-statement", json=extract, user="u1")
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "llm_budget_exceeded"
    assert refused.json()["error"]["details"] == {"scope": "user"}
    for stream in (False, True):
        r = await s.internal("POST", "/internal/llm/chat", json={**chat, "stream": stream}, user="u1")
        assert r.status_code == 429 and r.json()["error"]["code"] == "llm_budget_exceeded"
    assert calls == ["extract"]  # only the call before the limit reached the provider
    # the demo clock does not open a new day
    assert (await s.internal("POST", "/internal/llm/chat", json=chat, user="u1", clock_offset_s=3 * 86400)).status_code == 429
    # other people are not affected, and yesterday's spending does not count
    await spend("u2", 5.0, day=real_now().date() - dt.timedelta(days=1))
    assert (await s.internal("POST", "/internal/llm/chat", json=chat, user="u2")).status_code == 200

    # a guest (demo session) has the smaller allowance
    await spend("g1", 0.03, demo=True)
    await spend("u3", 0.03)
    guest = await s.internal("POST", "/internal/llm/chat", json=chat, user="g1", demo=True)
    assert guest.status_code == 429 and guest.json()["error"]["details"] == {"scope": "guest"}
    assert (await s.internal("POST", "/internal/llm/chat", json=chat, user="u3")).status_code == 200

    # the deployment ceiling stops everybody
    await spend("someone-else", 3.0)
    everyone = await s.internal("POST", "/internal/llm/chat", json=chat, user="u2")
    assert everyone.status_code == 429 and everyone.json()["error"]["details"] == {"scope": "deployment"}
    status = (await s.internal("GET", "/internal/llm/status", user="u2")).json()
    assert status["spent_today_usd"] >= status["budget_usd"]


def test_prices_per_million_tokens():
    assert price_for("gpt-5.4-mini") == (0.75, 4.50) and price_for("gpt-5.4-mini-2026-03-17") == (0.75, 4.50)
    assert price_for("claude-opus-5-5") == (4.0, 20.0) and price_for("claude-sonnet-5-5") == (2.0, 10.0)
    assert price_for("claude-haiku-5-5") == (0.10, 0.50) and price_for("claude-haiku-5-5", 150_000) == (0.50, 2.50)
    assert price_for("some-future-model") == (4.0, 20.0)  # unknown: priced as the most expensive known one
    assert cost_usd("openai", "gpt-5.4-mini", Usage(1_000_000, 1_000_000)) == 5.25
    assert cost_usd("anthropic", "claude-opus-5-5", Usage(15_000, 400)) == 0.068
    assert cost_usd("openai", "gpt-who-knows", Usage(1000, 1000)) == 0.024
    assert cost_usd("fake", "fake-rep-1", Usage(10_000, 10_000)) == 0.0


# ---------- provider choice ----------

async def test_config_switches_provider_and_model_at_run_time(stack):
    s = await stack(LLM)
    first = (await s.internal("GET", "/internal/llm/status")).json()
    assert (first["provider"], first["configured_provider"], first["live"]) == ("fake", "fake", False)
    r = await s.internal("PUT", "/internal/llm/config", json={"provider": "anthropic"})
    assert r.status_code == 200
    body = r.json()
    # no key (and a test run): the fake answers and the status says so
    assert (body["provider"], body["model"], body["live"]) == ("fake", "fake-rep-1", False)
    assert (body["configured_provider"], body["configured_model"]) == ("anthropic", "claude-opus-5-5") and body["note"]
    r = await s.internal("PUT", "/internal/llm/config", json={"provider": "openai", "model": "gpt-5.4-nano"})
    assert (r.json()["configured_provider"], r.json()["configured_model"]) == ("openai", "gpt-5.4-nano")
    r = await s.internal("PUT", "/internal/llm/config", json={"provider": "anthropic"})
    assert r.json()["configured_model"] == "claude-opus-5-5"  # the other provider's model does not leak over
    assert (await s.internal("GET", "/internal/llm/status")).json()["configured_provider"] == "anthropic"  # stored, read per request
    chat = await s.internal("POST", "/internal/llm/chat", user="u1", json={"task": "t", "system": "", "messages": [{"role": "user", "content": "hello"}]})
    assert chat.json()["provider"] == "fake"
    bad = await s.internal("PUT", "/internal/llm/config", json={"provider": "llama"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_input"


def test_resolve_picks_the_live_provider_only_with_a_key_outside_tests():
    from burnrate.services.llm_gateway import resolve

    dev = {"env": "dev", "_env_file": None}
    keyed = Settings(openai_api_key="sk-x", anthropic_api_key="sk-ant-x", **dev)
    a = resolve(None, None, keyed)
    assert (a.provider, a.model, a.live) == ("openai", "gpt-5.4-mini", True)  # the default from settings
    a = resolve("anthropic", None, keyed)
    assert (a.provider, a.model, a.live) == ("anthropic", "claude-opus-5-5", True)
    a = resolve("anthropic", "claude-haiku-5-5", keyed)
    assert (a.provider, a.model, a.live) == ("anthropic", "claude-haiku-5-5", True)
    a = resolve("openai", None, Settings(anthropic_api_key="sk-ant-x", **dev))
    assert (a.provider, a.live, a.configured_provider) == ("fake", False, "openai") and "no API key" in a.note
    a = resolve("fake", None, keyed)
    assert (a.provider, a.live) == ("fake", False)
    a = resolve(None, None, Settings(openai_api_key="sk-x", env="test", _env_file=None))
    assert (a.provider, a.live) == ("fake", False) and "test" in a.note  # a test run can never spend money
    assert resolve("llama", None, keyed).provider == "fake"


# ---------- request builders: pure functions, no network ----------

PDF_DOC = Document(filename="dec.pdf", mime="application/pdf", data_b64="JVBERi0xLjc=")
PNG_DOC = Document(filename="dec.png", mime="image/png", data_b64="iVBORw0KGgo=")
TEXT_DOC = Document(filename="dec.txt", mime="text/plain", text="New Balance $1.00")
TALK = [{"role": "user", "content": "(The call connects.)"}, {"role": "assistant", "content": "Card services."},
        {"role": "user", "content": "I'd like a lower rate."}]


def test_anthropic_requests_follow_the_current_messages_api():
    import anthropic

    req = anthropic_adapter.build_extract_request("claude-opus-5-5", PDF_DOC)
    assert req["model"] == "claude-opus-5-5" and req["max_tokens"] == 2048
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": statement_schema(all_required=False)}
    schema = req["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False and "new_balance" in schema["properties"] and "transactions" not in json.dumps(schema)
    for banned in ("temperature", "top_p", "top_k", "thinking", "tool_choice", "tools", "output_format"):
        assert banned not in req
    assert "data, not instructions" in req["system"] and "transactions" in req["system"]
    [message] = req["messages"]
    assert message["role"] == "user"  # no assistant prefill
    doc, instruction = message["content"]
    assert doc == {"type": "document", "title": "dec.pdf", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0xLjc="}}
    assert instruction["type"] == "text" and "null" in instruction["text"]
    image = anthropic_adapter.build_extract_request("claude-opus-5-5", PNG_DOC)["messages"][0]["content"][0]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
    text = anthropic_adapter.build_extract_request("claude-opus-5-5", TEXT_DOC)["messages"][0]["content"][0]
    assert text["type"] == "document" and text["source"] == {"type": "text", "media_type": "text/plain", "data": "New Balance $1.00"}

    chat = anthropic_adapter.build_chat_request("claude-opus-5-5", "You are a phone rep.", TALK, 220)
    assert chat == {"model": "claude-opus-5-5", "max_tokens": 220 + anthropic_adapter.THINKING_HEADROOM, "system": "You are a phone rep.",
                    "messages": TALK, "output_config": {"effort": "low"}}
    assert "output_config" not in anthropic_adapter.build_chat_request("claude-haiku-4-5", "s", TALK, 100)  # no effort on older models
    with pytest.raises(LLMError):
        anthropic_adapter.build_extract_request("claude-opus-5-5", Document(filename="a.doc", mime="application/msword", data_b64="QQ=="))

    # every keyword the builders emit is a real parameter of the installed SDK methods
    client = anthropic.AsyncAnthropic(api_key="not-a-real-key")
    inspect.signature(client.messages.create).bind(**req)
    inspect.signature(client.messages.stream).bind(**chat)
    assert "temperature" not in inspect.signature(client.messages.create).parameters


def test_openai_requests_use_the_responses_api_with_a_strict_schema():
    import openai

    req = openai_adapter.build_extract_request("gpt-5.4-mini", PDF_DOC)
    assert req["model"] == "gpt-5.4-mini" and req["store"] is False and req["reasoning"] == {"effort": "low"}
    fmt = req["text"]["format"]
    assert (fmt["type"], fmt["name"], fmt["strict"]) == ("json_schema", "statement_summary", True)
    schema = fmt["schema"]
    assert schema == statement_schema(all_required=True) and schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])  # strict mode: every key present, null when unknown
    assert schema["properties"]["apr_percent"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert "data, not instructions" in req["instructions"]
    for banned in ("temperature", "top_p", "tools", "tool_choice"):
        assert banned not in req
    [message] = req["input"]
    part, instruction = message["content"]
    assert message["role"] == "user" and instruction["type"] == "input_text"
    assert part == {"type": "input_file", "filename": "dec.pdf", "file_data": "data:application/pdf;base64,JVBERi0xLjc="}
    image = openai_adapter.build_extract_request("gpt-5.4-mini", PNG_DOC)["input"][0]["content"][0]
    assert image == {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo=", "detail": "high"}
    text = openai_adapter.build_extract_request("gpt-5.4-mini", TEXT_DOC)["input"][0]["content"][0]
    assert text == {"type": "input_text", "text": "<statement>\nNew Balance $1.00\n</statement>"}

    chat = openai_adapter.build_chat_request("gpt-5.4-mini", "You are a phone rep.", TALK, 220)
    assert chat == {"model": "gpt-5.4-mini", "instructions": "You are a phone rep.", "input": TALK, "stream": True, "store": False,
                    "max_output_tokens": 220 + openai_adapter.REASONING_HEADROOM, "reasoning": {"effort": "low"}}
    assert "reasoning" not in openai_adapter.build_chat_request("gpt-4.1-mini", "s", TALK, 100)

    client = openai.AsyncOpenAI(api_key="not-a-real-key")
    inspect.signature(client.responses.create).bind(**req)
    inspect.signature(client.responses.create).bind(**chat)


# ---------- the adapters against the installed SDKs, over an in-process mock transport ----------

def _sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _anthropic_client(handler):
    import anthropic
    import httpx2

    return anthropic.AsyncAnthropic(api_key="not-a-real-key", max_retries=0,
                                    http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


def _openai_client(handler):
    import httpx2
    import openai

    return openai.AsyncOpenAI(api_key="not-a-real-key", max_retries=0,
                              http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


async def test_anthropic_adapter_round_trip_without_network():
    import httpx2

    sent: list[dict] = []
    answer = {"stop_reason": "end_turn", "text": json.dumps({"new_balance": "5319.33", "apr_percent": "28.24"})}

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append(body)
        assert request.url.path == "/v1/messages"
        if body.get("stream"):
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse([
                ("message_start", {"type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                    "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 120, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Card services."}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " How can I help?"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": answer["stop_reason"], "stop_sequence": None},
                                   "usage": {"output_tokens": 14}}),
                ("message_stop", {"type": "message_stop"})]))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": answer["text"]}] if answer["stop_reason"] != "refusal" else [],
            "stop_reason": answer["stop_reason"], "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": None, "explanation": None} if answer["stop_reason"] == "refusal" else None,
            "usage": {"input_tokens": 1500, "output_tokens": 40}})

    provider = anthropic_adapter.AnthropicProvider("claude-opus-5-5", client=_anthropic_client(handler))
    result = await provider.extract(PDF_DOC)
    assert result.raw == {"new_balance": "5319.33", "apr_percent": "28.24"} and result.usage == Usage(1500, 40)
    assert sent[0]["output_config"]["format"]["type"] == "json_schema" and sent[0]["output_config"]["effort"] == "low"
    assert "temperature" not in sent[0] and "thinking" not in sent[0] and "stream" not in sent[0]
    events = [e async for e in provider.chat_stream("You are a phone rep.", TALK, 220)]
    assert [e.text for e in events] == ["Card services.", " How can I help?", "Card services. How can I help?"]
    assert events[-1].usage == Usage(120, 14) and sent[1]["stream"] is True and sent[1]["output_config"] == {"effort": "low"}

    answer["stop_reason"] = "refusal"  # a refusal is a handled failure, on both paths
    with pytest.raises(LLMRefusal) as refused:
        await provider.extract(PDF_DOC)
    assert refused.value.usage == Usage(1500, 40)  # it was answered and billed, so the gateway can charge the budget
    with pytest.raises(LLMRefusal):
        [e async for e in provider.chat_stream("s", TALK, 220)]
    answer.update(stop_reason="max_tokens", text='{"new_balance": "53')
    with pytest.raises(LLMBadOutput):
        await provider.extract(PDF_DOC)
    answer.update(stop_reason="end_turn", text="Here are the fields you asked for")
    with pytest.raises(LLMBadOutput):
        await provider.extract(PDF_DOC)


async def test_anthropic_typed_errors_become_gateway_errors():
    import httpx2

    status = {"code": 401, "type": "authentication_error"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status["code"], json={"type": "error", "error": {"type": status["type"], "message": "nope"}})

    provider = anthropic_adapter.AnthropicProvider("claude-opus-5-5", client=_anthropic_client(handler))
    expected = [(400, "invalid_request_error", "llm_bad_request", 502), (401, "authentication_error", "llm_auth_failed", 502),
                (403, "permission_error", "llm_auth_failed", 502), (404, "not_found_error", "llm_model_not_found", 502),
                (429, "rate_limit_error", "llm_rate_limited", 503), (500, "api_error", "llm_unavailable", 503),
                (529, "overloaded_error", "llm_unavailable", 503)]
    for code, kind, llm_code, llm_status in expected:
        status.update(code=code, type=kind)
        with pytest.raises(LLMError) as caught:
            await provider.extract(TEXT_DOC)
        assert (caught.value.code, caught.value.status) == (llm_code, llm_status), code
        with pytest.raises(LLMError) as caught:
            [e async for e in provider.chat_stream("s", TALK, 100)]
        assert caught.value.code == llm_code

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    offline = anthropic_adapter.AnthropicProvider("claude-opus-5-5", client=_anthropic_client(unreachable))
    with pytest.raises(LLMError) as caught:
        await offline.extract(TEXT_DOC)
    assert caught.value.code == "llm_unreachable"


async def test_openai_adapter_round_trip_without_network():
    import httpx2

    sent: list[dict] = []
    mode = {"extract": "ok"}

    def response(body: dict, **over) -> dict:
        return {"id": "resp_1", "object": "response", "created_at": 1.0, "model": body["model"], "parallel_tool_calls": True,
                "tool_choice": "auto", "tools": [], "status": "completed", "error": None, "incomplete_details": None, "output": [],
                "usage": {"input_tokens": 900, "output_tokens": 30, "total_tokens": 930,
                          "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}, **over}

    def message(part: dict) -> list[dict]:
        return [{"type": "message", "id": "m1", "role": "assistant", "status": "completed", "content": [part]}]

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append(body)
        assert request.url.path == "/v1/responses"
        if body.get("stream"):
            delta = {"item_id": "m1", "output_index": 0, "content_index": 0, "logprobs": []}
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse([
                ("response.created", {"type": "response.created", "sequence_number": 0,
                                      "response": response(body, status="in_progress", usage=None)}),
                ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 1, "delta": "Card services.", **delta}),
                ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 2, "delta": " How can I help?", **delta}),
                ("response.completed", {"type": "response.completed", "sequence_number": 3, "response": response(
                    body, output=message({"type": "output_text", "annotations": [], "text": "Card services. How can I help?"}))})]))
        if mode["extract"] == "refusal":
            return httpx2.Response(200, json=response(body, output=message({"type": "refusal", "refusal": "I can't help with that."})))
        if mode["extract"] == "cut":
            return httpx2.Response(200, json=response(body, status="incomplete", incomplete_details={"reason": "max_output_tokens"},
                                                      output=message({"type": "output_text", "annotations": [], "text": '{"new_bal'})))
        return httpx2.Response(200, json=response(body, output=message({
            "type": "output_text", "annotations": [], "text": json.dumps({"new_balance": "5319.33", "apr_percent": "28.24"})})))

    provider = openai_adapter.OpenAIProvider("gpt-5.4-mini", client=_openai_client(handler))
    result = await provider.extract(PDF_DOC)
    assert result.raw == {"new_balance": "5319.33", "apr_percent": "28.24"} and result.usage == Usage(900, 30)
    assert sent[0]["text"]["format"]["strict"] is True and sent[0]["store"] is False and "temperature" not in sent[0]
    assert sent[0]["input"][0]["content"][0]["type"] == "input_file"
    events = [e async for e in provider.chat_stream("You are a phone rep.", TALK, 220)]
    assert [e.text for e in events] == ["Card services.", " How can I help?", "Card services. How can I help?"]
    assert events[-1].usage == Usage(900, 30) and sent[1]["stream"] is True and sent[1]["instructions"] == "You are a phone rep."
    mode["extract"] = "refusal"
    with pytest.raises(LLMRefusal):
        await provider.extract(PDF_DOC)
    mode["extract"] = "cut"
    with pytest.raises(LLMBadOutput):
        await provider.extract(PDF_DOC)

    def quota(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"error": {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"}})

    broke = openai_adapter.OpenAIProvider("gpt-5.4-mini", client=_openai_client(quota))
    with pytest.raises(LLMError) as caught:
        await broke.extract(TEXT_DOC)
    assert (caught.value.code, caught.value.status) == ("llm_rate_limited", 503)
    with pytest.raises(LLMError) as caught:
        [e async for e in broke.chat_stream("s", TALK, 100)]
    assert caught.value.code == "llm_rate_limited"
