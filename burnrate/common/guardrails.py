"""Wording rules from the product spec, section 3.16. One pure function, used on every generated string:
interface copy, scripts, emails and model replies.

Burn Rate is self-help software: the person makes the call. So the text may never promise a result, speak as an
intermediary, or say anything about improving a credit score. When the caller passes `allowed_numbers`, every
dollar amount and percentage in the text must be one of them (the engine's numbers), whatever way it is written.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel


class Violation(BaseModel):
    rule: str
    match: str


class GuardResult(BaseModel):
    ok: bool
    violations: list[Violation] = []


# Who is speaking when the product talks about itself.
_US = r"(?:we|burn\s*rate|our\s+(?:team|experts?|agents?|specialists?|negotiators?|service|app|ai))"
# Words that may sit between the subject and the verb: "we will", "we'll also", "we can always".
_AUX = (r"(?:(?:\s+|')(?:will|ll|can|could|would|do|did|have|has|had|are|were|shall|may|might|also|always|already|"
        r"just|now|then|even|actually|simply|help(?:s|ed)?(?:\s+you)?|going\s+to|gonna|able\s+to|get\s+to|ve|re))*")
_RATE_THING = (r"(?:(?:credit\s+card|card|interest|purchase|monthly|annual|late|current|high|existing)\s+){0,2}"
               r"(?:rates?|aprs?|interest|bills?|payments?|fees?|debts?|balances?)")
_CREDIT_THING = r"credit\s*(?:scores?|ratings?|reports?|history|histories|profiles?|files?|standing|worthiness)|fico(?:\s+scores?)?"
_UP_VERB = (r"(?:improv|rais|boost|increas|lift|build|rebuild|fix|repair|restor|enhanc|grow|bump|elevat|strengthen|"
            r"clean)(?:e|es|ed|ing|s|t)?(?:\s+up)?")

_RULES: list[tuple[str, str, list[str]]] = [
    ("we_lower_your_rate", "The product does not change anyone's rate; the cardholder asks and the issuer decides.", [
        rf"\b{_US}{_AUX}\s+(?:lower|reduce|cut|slash|drop|decrease|shrink|bring\s+down)(?:s|d|ed|ped|ting|ping|ing)?"
        rf"\s+(?:your|the|their|my)?\s*{_RATE_THING}\b",
        rf"\b{_US}{_AUX}\s+get(?:s|ting)?\s+(?:you|your)\s+(?:a\s+)?(?:lower|better|reduced)\s+{_RATE_THING}\b",
    ]),
    ("we_negotiate_for_you", "The product is not an intermediary and does not negotiate with anyone.", [
        rf"\b{_US}{_AUX}\s+negotiat(?:e|es|ed|ing)\b",
        r"\bnegotiat(?:e|es|ed|ing|ion|ions)\s+(?:\w+\s+){0,4}?(?:for\s+you|on\s+your\s+behalf|in\s+your\s+name)\b",
        r"\b(?:on\s+your\s+behalf|for\s+you)\s*,?\s+(?:we|our\s+team)\s+(?:will\s+)?negotiat(?:e|es|ed|ing)\b",
        r"\bnegotiation\s+service\b",
    ]),
    ("we_will_call", "Nobody at Burn Rate calls an issuer or a customer; there is no phone channel.", [
        rf"(?<!what\s)\b{_US}{_AUX}\s+(?:call|phone|ring|dial)(?:s|ed|ing)?\b(?!\s+(?:it|this|that|these|those)\b)",
        rf"\b{_US}{_AUX}\s+(?:contact|reach\s+out\s+to|speak\s+(?:to|with)|talk\s+(?:to|with))(?:s|ed|ing)?"
        r"\s+(?:your|the)\s+(?:\w+\s+){0,2}?(?:bank|issuer|card\s+compan(?:y|ies)|creditors?|lenders?)\b",
        r"\blet\s+us\s+(?:make\s+the\s+)?call\b",
    ]),
    ("debt_relief", "'Debt relief' names a regulated service the product is not.", [
        r"\bdebt[\s-]*relief\b",
        r"\brelief\s+from\s+(?:your\s+|credit\s+card\s+)?debts?\b",
        r"\bdebt[\s-]*(?:forgiveness|elimination|negotiation|resolution)\b",
    ]),
    ("settle_your_debt", "Settling debt is a regulated service the product is not.", [
        r"\bsettl(?:e|es|ed|ing)\s+(?:(?:your|my|the|their|a|an|any|all|this|that|those|these|credit|card)\s+){0,3}"
        r"(?:debts?|balances?|accounts?|cards?|bills?|loans?)\b",
        r"\bdebts?\s+settl(?:ement|ements|ed|ing)\b",
        r"\bsettlement\s+(?:offers?|programs?|compan(?:y|ies)|services?|plans?)\b",
        r"\bsettl(?:e|es|ed|ing)\s+for\s+(?:less|pennies|a\s+fraction)\b",
    ]),
    ("guaranteed", "No result is guaranteed; the issuer may say no.", [
        r"\bguaranteed\b",
        r"\bguarantee(?:s|ing)?\s+(?:you|your|results?|savings?|approval|success|(?:a\s+)?lower|(?:a\s+)?better)\b",
        rf"\b(?:{_US}|i|they|it|this|that|which|issuers?|banks?){_AUX}\s+guarantee(?:s|ing)?\b",
    ]),
    ("you_will_save", "A saving is arithmetic with visible assumptions, never a promise.", [
        r"\byou(?:\s+will|'ll|\s+are\s+going\s+to|'re\s+going\s+to|\s+are\s+sure\s+to|\s+can\s+expect\s+to|\s+shall)"
        r"(?:\s+(?:be|definitely|certainly|surely|always|also|soon|easily))*\s+sav(?:e|ing)\b",
        r"\byou(?:'re|\s+are)\s+guaranteed\b",
    ]),
    ("typical_savings", "Typical or average savings need a representative sample the product does not have.", [
        r"\b(?:typical|average|usual|normal|expected|median)\s+(?:\w+\s+){0,2}?sav(?:ing|ings)\b",
        r"\b(?:typically|usually|normally|generally|on\s+average)\s+sav(?:e|es|ed)\b",
        r"\bsav(?:e|es|ed|ing|ings)\s+(?:of\s+)?(?:an\s+average\s+of|on\s+average)\b",
        r"\b(?:users?|members?|customers?|people|cardholders?)\s+(?:typically\s+|usually\s+|on\s+average\s+)?sav(?:e|es|ed)\s+"
        r"(?:an\s+average|\$|\d)",
    ]),
    ("credit_score_claim", "Nothing about improving a credit score, report or rating (credit repair law).", [
        rf"\b{_UP_VERB}\s+(?:\w+\s+){{0,3}}?(?:{_CREDIT_THING})\b",
        rf"\b(?:{_CREDIT_THING})\s+(?:\w+\s+){{0,3}}?(?:go(?:es)?\s+up|going\s+up|ris(?:e|es|ing)|climb(?:s|ing)?|"
        r"jump(?:s|ing)?|improv(?:e|es|ed|ing|ement)|increas(?:e|es|ed|ing)|recover(?:s|ed|ing)?|boost(?:s|ed)?)\b",
        rf"\b(?:higher|better|stronger|improved|healthier)\s+(?:{_CREDIT_THING})\b",
        rf"\b(?:good|great|help(?:s|ed)?|helpful)\s+for\s+(?:your\s+)?(?:{_CREDIT_THING})\b",
        rf"\bhelp(?:s|ed|ing)?\s+(?:your\s+)?(?:{_CREDIT_THING})\b",
        r"\b(?:improv|build|rebuild|boost|rais)(?:e|es|ed|ing|s)?\s+(?:your|my|their)\s+credit\b(?!\s*(?:cards?|limit|line)\b)",
    ]),
    ("credit_repair", "Credit repair is a regulated service the product is not.", [
        r"\bcredit[\s-]*repair\b",
        r"\b(?:repair|fix|rebuild|restore|clean\s+up)(?:s|ed|ing)?\s+(?:your|my|their|the)?\s*(?:bad\s+|poor\s+|damaged\s+)?credit\b"
        r"(?!\s*(?:cards?|limit|line)\b)",
    ]),
    ("professional_negotiator", "The product may not present itself, or its AI, as a professional negotiator.", [
        r"\b(?:as|like)\s+(?:a|an|your|your\s+own|your\s+personal)\s+(?:\w+\s+){0,2}?negotiator\b",
        r"\b(?:professional|expert|certified|trained|experienced|seasoned|skilled)\s+negotiat(?:or|ors|ion|ions|ing)\b",
        r"\bnegotiat(?:ion|ing)\s+(?:experts?|professionals?|specialists?|pros?)\b",
        r"\bour\s+negotiators?\b",
    ]),
]

UNLISTED_NUMBER = "unlisted_number"
RULES: tuple[str, ...] = tuple(name for name, _, _ in _RULES) + (UNLISTED_NUMBER,)
RULE_DESCRIPTIONS: dict[str, str] = {name: text for name, text, _ in _RULES} | {
    UNLISTED_NUMBER: "Every dollar amount and percentage must be one of the numbers the caller allowed.",
}
_COMPILED = [(name, [re.compile(p, re.IGNORECASE) for p in patterns]) for name, _, patterns in _RULES]

_NUM = r"\d[\d,]*(?:\.\d+)?|\.\d+"
_DOLLAR_SIGN = re.compile(rf"(?:US\s*)?\$\s*({_NUM})(?:\s*(k|thousand|m|million)\b)?", re.IGNORECASE)
_DOLLAR_WORD = re.compile(rf"(?<![\w$.,])({_NUM})(?:\s*(k|thousand|million))?\s*(?:dollars?|usd|bucks)\b", re.IGNORECASE)
_CENTS_WORD = re.compile(rf"(?<![\w$.,])({_NUM})\s*(?:cents?\b|¢)", re.IGNORECASE)
_PERCENT = re.compile(rf"(?<![\w$.,])({_NUM})\s*(?:%|percent(?:age\s+points?)?\b|per\s+cent\b|pct\b)", re.IGNORECASE)
_BARE = re.compile(rf"^\s*({_NUM})\s*$")
_SCALE = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}
_CLEAN = str.maketrans({" ": " ", " ": " ", " ": " ", "’": "'", "‘": "'", "ʼ": "'",
                        "​": None, "‌": None, "‍": None, "﻿": None, "­": None})


def _canon(raw: str, scale: int | Decimal = 1) -> str | None:
    try:
        d = Decimal(raw.replace(",", "")) * scale
    except InvalidOperation:
        return None
    d = d.normalize()
    return format(d, "f") if d != 0 else "0"


def _numbers(text: str) -> list[tuple[str, str]]:
    """(canonical form, text as written) for every dollar amount and percentage, in reading order."""
    found: list[tuple[int, str, str]] = []
    for m in _DOLLAR_SIGN.finditer(text):
        c = _canon(m.group(1), _SCALE.get((m.group(2) or "").lower(), 1))
        if c is not None:
            found.append((m.start(), f"${c}", m.group(0).strip()))
    for m in _DOLLAR_WORD.finditer(text):
        c = _canon(m.group(1), _SCALE.get((m.group(2) or "").lower(), 1))
        if c is not None:
            found.append((m.start(), f"${c}", m.group(0).strip()))
    for m in _CENTS_WORD.finditer(text):
        c = _canon(m.group(1), Decimal("0.01"))
        if c is not None:
            found.append((m.start(), f"${c}", m.group(0).strip()))
    for m in _PERCENT.finditer(text):
        c = _canon(m.group(1))
        if c is not None:
            found.append((m.start(), f"{c}%", m.group(0).strip()))
    return [(c, raw) for _, c, raw in sorted(found)]


def normalize_number(value: str) -> set[str]:
    """Canonical forms of one allowed entry. "$240", "$240.00" and "240 dollars" give {"$240"};
    "27.99 percent" gives {"27.99%"}; a bare "240" allows both the amount and the percentage."""
    text = str(value).translate(_CLEAN)
    forms = {c for c, _ in _numbers(text)}
    if not forms:
        bare = _BARE.match(text)
        c = _canon(bare.group(1)) if bare else None
        if c is not None:
            forms = {f"${c}", f"{c}%"}
    return forms


def check_text(text: str, *, allowed_numbers: set[str] | None = None) -> GuardResult:
    clean = (text or "").translate(_CLEAN)
    violations: list[Violation] = []
    for name, patterns in _COMPILED:
        seen: set[str] = set()
        for pattern in patterns:
            for m in pattern.finditer(clean):
                hit = " ".join(m.group(0).split())
                if hit.lower() not in seen:
                    seen.add(hit.lower())
                    violations.append(Violation(rule=name, match=hit))
    if allowed_numbers is not None:
        allowed: set[str] = set()
        for entry in allowed_numbers:
            allowed |= normalize_number(entry)
        seen_numbers: set[str] = set()
        for canonical, raw in _numbers(clean):
            if canonical not in allowed and canonical not in seen_numbers:
                seen_numbers.add(canonical)
                violations.append(Violation(rule=UNLISTED_NUMBER, match=raw))
    return GuardResult(ok=not violations, violations=violations)
