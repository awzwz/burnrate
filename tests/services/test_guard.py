import pytest

from burnrate.common.guardrails import RULES, check_text, normalize_number

FORBIDDEN = [
    ("We lower your rate.", "we_lower_your_rate"),
    ("we'll   lower your APR for you", "we_lower_your_rate"),
    ("We lowered your interest rate last month", "we_lower_your_rate"),
    ("Burn Rate cuts your interest", "we_lower_your_rate"),
    ("We help you lower your rate", "we_lower_your_rate"),
    ("WE  NEGOTIATE  FOR  YOU", "we_negotiate_for_you"),
    ("Our experts will negotiate on your behalf", "we_negotiate_for_you"),
    ("We negotiate with your bank", "we_negotiate_for_you"),
    ("Relax, we will call.", "we_will_call"),
    ("We’ll call your issuer", "we_will_call"),
    ("Our team contacts your bank", "we_will_call"),
    ("Get debt relief today", "debt_relief"),
    ("A debt-relief plan", "debt_relief"),
    ("Settle your debt for less", "settle_your_debt"),
    ("We settled their credit card debts", "settle_your_debt"),
    ("A debt settlement program", "settle_your_debt"),
    ("Guaranteed results", "guaranteed"),
    ("We guarantee a lower rate", "guaranteed"),
    ("You will save $600 a year", "you_will_save"),
    ("you'll definitely save", "you_will_save"),
    ("Typical savings: $630", "typical_savings"),
    ("Members typically save $400", "typical_savings"),
    ("This will improve your credit score", "credit_score_claim"),
    ("Raising your credit score is easy", "credit_score_claim"),
    ("Your credit score will go up", "credit_score_claim"),
    ("Boost your FICO score", "credit_score_claim"),
    ("credit repair", "credit_repair"),
    ("Fix your credit fast", "credit_repair"),
    ("As a professional negotiator, the app talks to the bank", "professional_negotiator"),
    ("It works like an expert negotiator", "professional_negotiator"),
]

ALLOWED = [
    "See what your cards cost you per day.",
    "Estimates from the numbers you entered.",
    "Wording other cardholders use; the issuer may say no.",
    "Will this request result in a hard inquiry?",
    "Is the new rate permanent or temporary, and until what date?",
    "Ask Capital One to lower your rate",
    "I would like to ask you to lower my purchase APR.",
    "How will the account be reported to the credit bureaus?",
    "Each point off is worth $52 a year on this balance",
    "Rate lowered from 24.49% to 22.49%",
    "Refused. Ask again on Jan 15.",
    "This is what we call your burn rate.",
    "Refund any time inside the 14-day money-back guarantee window.",
    "Fix credit card errors on your statement.",
    "Let me check what I can do on this account.",
]


@pytest.mark.parametrize("text,rule", FORBIDDEN)
def test_promises_and_intermediary_language_are_rejected(text, rule):
    result = check_text(text)
    assert not result.ok
    assert rule in {v.rule for v in result.violations}, result.violations
    assert all(v.match for v in result.violations)


@pytest.mark.parametrize("text", ALLOWED)
def test_self_help_wording_passes(text):
    result = check_text(text)
    assert result.ok and result.violations == [], result.violations


def test_the_rule_list_is_exported_and_complete():
    assert {rule for _, rule in FORBIDDEN} | {"unlisted_number"} == set(RULES)


def test_numbers_are_compared_after_normalisation():
    assert normalize_number("$240") == normalize_number("$240.00") == normalize_number("240 dollars") == {"$240"}
    assert normalize_number("27.99%") == normalize_number("27.99 percent") == {"27.99%"}
    assert normalize_number("240") == {"$240", "240%"}  # a bare number allows either reading
    text = "Send $240.00 to the card at 27.99 percent. That is 240 dollars before the due date."
    assert check_text(text, allowed_numbers={"$240", "27.99%"}).ok
    assert check_text(text, allowed_numbers={"240 dollars", "27.99 percent"}).ok
    assert check_text("Balance $5,200.00 at 24.5%.", allowed_numbers={"$5200", "24.50%"}).ok


def test_a_number_that_was_not_allowed_is_a_violation():
    result = check_text("Send $240 today and the rate drops to 19.99%, about $300 a year.", allowed_numbers={"$240"})
    assert [(v.rule, v.match) for v in result.violations] == [("unlisted_number", "19.99%"), ("unlisted_number", "$300")]
    assert not check_text("It costs 50 cents a day or 5k dollars a decade.", allowed_numbers=set()).ok
    # without a list, numbers are not checked at all
    assert check_text("Send $240 today at 19.99%.").ok
    # plain counts are not money or percentages
    assert check_text("Ask again in 6 months, on day 14.", allowed_numbers=set()).ok


async def test_guard_service_checks_text_and_lists_rules(stack):
    s = await stack(["guard"])
    ok = await s.internal("POST", "/internal/guard/check", json={"text": "See what your cards cost you per day."})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "violations": []}
    bad = (await s.internal("POST", "/internal/guard/check", json={"text": "Guaranteed: we lower your rate."})).json()
    assert bad["ok"] is False and {v["rule"] for v in bad["violations"]} == {"guaranteed", "we_lower_your_rate"}
    assert all(set(v) == {"rule", "match"} for v in bad["violations"])
    nums = (await s.internal("POST", "/internal/guard/check",
                             json={"text": "Pay $240.00 at 27.99 percent, not $250.", "allowed_numbers": ["$240", "27.99%"]})).json()
    assert nums == {"ok": False, "violations": [{"rule": "unlisted_number", "match": "$250"}]}
    rules = (await s.internal("GET", "/internal/guard/rules")).json()["rules"]
    assert [r["name"] for r in rules] == list(RULES) and all(r["description"] for r in rules)
    assert (await s.client.post("/internal/guard/check", json={"text": "x"})).status_code == 403
