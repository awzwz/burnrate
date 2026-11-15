"""The fake provider: deterministic, free, offline. Every test runs on it, and so does a deployment with no API key.

Statements: plain text is parsed with regular expressions for the standard summary labels; a PDF or an image gets a
fixed, arithmetically consistent sample. Chat: a scripted card-issuer representative that picks its lines from
keywords in the caller's last message and from the scenario named in the system prompt.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import re
from collections.abc import AsyncIterator

from burnrate.common.config import settings
from burnrate.common.context import today
from burnrate.common.llm.base import ChatDelta, ChatDone, Document, ExtractResult, Usage, estimate_tokens

MODEL = "fake-rep-1"

_MONEY = r"(\(?-?\s*\$?\s*\d[\d,]*(?:\.\d{1,2})?\)?(?:\s*CR\b)?)"
_DATE = r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})"
_GAP = r"[^\d$(\-\n]*"  # dots, colons, spaces and words between a label and its figure
_AT = r"(?:^[ \t]*|(?<=[ \t]{2})|(?<=\t))"  # a label starts a line or a column, so "Total Interest Charged" is not "Interest Charged"
_LABELS: dict[str, str] = {
    "new_balance": rf"{_AT}New\s+Balance\b{_GAP}{_MONEY}",
    "previous_balance": rf"{_AT}Previous\s+Balance\b{_GAP}{_MONEY}",
    "payments_credits": rf"{_AT}Payments\b{_GAP}{_MONEY}",
    "purchases": rf"{_AT}Purchases\b{_GAP}{_MONEY}",
    "fees": rf"{_AT}Fees\s+Charged\b{_GAP}{_MONEY}",
    "interest": rf"{_AT}Interest\s+Charged\b{_GAP}{_MONEY}",
    "balance_subject_to_interest": rf"{_AT}Balance\s+Subject\s+to\s+Interest(?:\s+Rate)?\b{_GAP}{_MONEY}",
    "min_payment": rf"{_AT}(?:Total\s+)?Minimum\s+Payment\s+Due\b{_GAP}{_MONEY}",
    "late_fee": rf"{_AT}Late\s+(?:Payment\s+)?Fee\b{_GAP}{_MONEY}",
    "interest_ytd": rf"{_AT}Total\s+Interest\s+Charged\s+in\s+\d{{4}}\b{_GAP}{_MONEY}",
    "apr_percent": r"Annual\s+Percentage\s+Rate(?:\s*\(APR\))?[^\d\n]*?(\d{1,2}(?:\.\d{1,3})?)\s*%",
    "days_in_cycle": r"Days\s+in\s+Billing\s+(?:Cycle|Period)\b[^\d\n]*(\d{1,2})\b",
    "due_date": rf"Payment\s+Due\s+Date\b[^\w\n]*{_DATE}",
    "close_date": rf"(?:Statement\s+)?Closing\s+Date\b[^\w\n]*{_DATE}",
}
_COMPILED = {name: re.compile(pattern, re.IGNORECASE | re.MULTILINE) for name, pattern in _LABELS.items()}


def parse_statement_text(text: str) -> dict:
    """Figures as printed, keyed the way a model would return them. Unknown labels are simply absent."""
    out: dict = {}
    for name, pattern in _COMPILED.items():
        m = pattern.search(text)
        if m:
            out[name] = int(m.group(1)) if name == "days_in_cycle" else m.group(1).strip()
    return out


def sample_statement(on: dt.date) -> dict:
    """What the fake "reads" from a PDF or a photo: $5,120.00 at 27.99% for 30 days is $117.79 of interest."""
    close = on - dt.timedelta(days=3)
    return {"previous_balance": "5200.00", "payments_credits": "240.00", "purchases": "0.00", "fees": "0.00",
            "interest": "117.79", "new_balance": "5077.79", "apr_percent": "27.99", "balance_subject_to_interest": "5120.00",
            "days_in_cycle": 30, "min_payment": "168.57", "close_date": close.isoformat(),
            "due_date": (close + dt.timedelta(days=25)).isoformat()}


# ---------- the scripted representative ----------

KICKOFF_MARK = "(the call connects"
_OPENING = "Thank you for calling card services, this is Jordan. How can I help you today?"
_TOPICS: dict[str, str] = {
    "supervisor": r"supervisor|manager|retention|someone else|escalate|cancel (?:my|the|this) (?:card|account)|close (?:my|the|this) (?:card|account)",
    "hard_inquiry": r"hard (?:inquiry|pull|credit)|credit (?:check|inquiry|pull)|(?:pull|check|run) my credit",
    "permanent": r"permanent|temporary|how long|until (?:what|when)|expire|end date|go back",
    "balances": r"which balances?|existing balance|current balance|new purchases|apply to",
    "qualify": r"qualify|eligible|ask again|try again|what would (?:it take|help)|call back",
    "on_statement": r"next statement|show (?:up )?on|appear on|in writing|confirmation",
    "hardship": r"hardship|can'?t (?:afford|cover|make|pay)|trouble (?:paying|covering|making)|lost my job",
    "due_date": r"due date|move (?:my|the) (?:payment|due)",
    "fee": r"(?:refund|waive|remove|reverse|take off|credit back|forgive)\b[^.?!]{0,40}\bfee|\bfee\b[^.?!]{0,40}\b(?:refund|waived?|removed?|reversed?|back)"
           r"|late fee|annual fee",
    "rate": r"(?:lower|reduce|reduction|decrease|drop|cut|better|bring down)\b[^.?!]{0,40}\b(?:rate|apr|interest)"
            r"|\b(?:rate|apr|interest)\b[^.?!]{0,30}\b(?:lowered|reduced|reduction|down)",
    "accept": r"\b(?:yes|yeah|sure|please do|go ahead|apply (?:it|that)|i accept|i'll take|sounds good|okay|ok)\b",
    "thanks": r"\bthank(?:s| you)?\b|appreciate",
    "greeting": r"^\W*(?:hi|hello|hey|good (?:morning|afternoon|evening))\b",
}
_TOPIC_RE = {k: re.compile(v, re.IGNORECASE) for k, v in _TOPICS.items()}
_TRANSFER = ("That request is handled by the account specialist team, so I'll transfer you now. Please hold. "
             "Thank you for holding, this is Riley with the account specialist team. What can I do for you?")
_ASKS: dict[str, dict[str, str]] = {
    "rate": {
        "agreeable": "Thank you for asking. I've reviewed the account and I'm able to lower the purchase rate by two "
                     "percentage points. Would you like me to apply that today?",
        "temporary_offer": "I'm not able to change the standard rate today. What I can offer is a rate three points lower "
                           "for the next six billing cycles. Would you like to hear the details?",
        "refusal": "I've looked at the account, and I'm not able to change the rate today.",
    },
    "fee": {
        "agreeable": "I see the fee on the account. As a one-time courtesy I can refund it, and the credit will appear "
                     "on your next statement.",
        "temporary_offer": "I can't refund the full fee, but as a courtesy I can credit half of it. It will appear on "
                           "your next statement.",
        "refusal": "I'm sorry, I'm not able to remove that fee today.",
    },
    "due_date": {
        "agreeable": "I can move the due date for you. Which day of the month works best?",
        "temporary_offer": "I can move the due date, but the change starts two billing cycles from now. Would that work?",
        "refusal": "I'm sorry, I'm not able to change the due date on this account today.",
    },
    "hardship": {
        "agreeable": "I'm sorry to hear that. There is a hardship arrangement I can describe: a reduced rate for a set "
                     "number of months. Would you like to go through the terms?",
        "temporary_offer": "I'm sorry to hear that. I can describe a short-term arrangement with a reduced rate for six "
                           "months. Would you like to go through the terms?",
        "refusal": "I'm sorry to hear that. I don't have an arrangement to offer on this account today.",
    },
}
_ANSWERS: dict[str, dict[str, str]] = {
    "hard_inquiry": {"*": "No, this request doesn't involve a hard inquiry."},
    "permanent": {
        "agreeable": "It's a permanent change to the purchase rate, with no end date.",
        "temporary_offer": "It's temporary: it lasts six billing cycles, then the rate returns to the current one.",
        "refusal": "Nothing changes on the account today, so there is no end date to give you.",
    },
    "balances": {"*": "It applies to your existing purchase balance and to new purchases."},
    "qualify": {"*": "A longer record of on-time payments would help. You're welcome to ask again after your next "
                     "six statements."},
    "on_statement": {"*": "Yes, you'll see it on your next statement, and a written confirmation goes out as well."},
}


def scripted_reply(system: str, messages: list[dict]) -> str:
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if KICKOFF_MARK in last.lower():
        return _OPENING
    found = re.search(r"scenario:\s*([a-z_]+)", system or "", re.IGNORECASE)
    scenario = found.group(1).lower() if found else "agreeable"
    said = " ".join(m["content"] for m in messages if m["role"] == "assistant")
    transferred = "transfer you" in said
    if scenario == "transfer" and transferred:
        scenario = "agreeable"  # the specialist who picked up can decide
    if scenario not in ("agreeable", "temporary_offer", "refusal", "transfer"):
        scenario = "agreeable"
    topics = [name for name, pattern in _TOPIC_RE.items() if pattern.search(last)]
    if "supervisor" in topics and not transferred:
        return _TRANSFER
    parts: list[str] = []
    ask = next((t for t in ("hardship", "due_date", "fee", "rate") if t in topics), None)
    if ask:
        if scenario == "transfer":
            return _TRANSFER
        line = _ASKS[ask][scenario]
        if line not in said:  # do not repeat an offer that is already on the table
            parts.append(line)
    for topic in ("permanent", "balances", "hard_inquiry", "on_statement", "qualify"):
        if topic in topics:
            answers = _ANSWERS[topic]
            parts.append(answers.get(scenario) or answers.get("*") or answers["agreeable"])
    if parts:
        return " ".join(parts[:3])
    if "accept" in topics and scenario != "refusal":
        return "All right, that's done. You'll see the change on your next statement. Is there anything else?"
    if "thanks" in topics:
        return "You're welcome. Is there anything else I can help you with today?"
    if "greeting" in topics:
        return "Hello. What can I help you with on your account today?"
    return "I understand. Could you tell me a little more about what you'd like me to do on this account?"


class FakeProvider:
    name = "fake"
    model = MODEL

    async def extract(self, doc: Document) -> ExtractResult:
        text = doc.text
        if text is None and doc.data_b64 and doc.mime.startswith("text/"):
            text = base64.b64decode(doc.data_b64).decode("utf-8", errors="replace")
        raw = parse_statement_text(text) if text is not None else sample_statement(today())
        return ExtractResult(raw=raw, usage=Usage(estimate_tokens(text or ""), 60))

    async def chat_stream(self, system: str, messages: list[dict], max_tokens: int) -> AsyncIterator[ChatDelta | ChatDone]:
        reply = scripted_reply(system, messages)
        pause = 0.0 if settings().is_test else 0.03
        words = reply.split(" ")
        for i, word in enumerate(words):
            yield ChatDelta(word if i == 0 else " " + word)
            await asyncio.sleep(pause)
        usage = Usage(estimate_tokens(system, *[m["content"] for m in messages]), estimate_tokens(reply))
        yield ChatDone(text=reply, usage=usage)
