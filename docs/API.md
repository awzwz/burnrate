# Burn Rate public API (edge-api)

Base path `/api`. JSON in and out. The browser is identified by an HttpOnly cookie `br_session` that `POST /api/session`
sets; send requests with `credentials: "include"`. No login is needed to use the product in demo mode.

**Conventions**
- Money is an integer number of cents (`*_cents`). Rates are basis points (`apr_bps`: 27.99% = 2799).
- `burn_per_day`, `burn_before`, `burn_after`, `burn_cut` are **cents per day as a float** (738.3425 = $7.38 a day).
- Dates are `YYYY-MM-DD` in the user's time zone; timestamps are ISO 8601 UTC.
- Errors: HTTP status plus `{"error": {"code": "...", "message": "human text", "details": {...}?}}`.
  `401 no_session`, `402 subscription_required`, `404 not_found`, `409 conflict`, `422 invalid_input`, `429 rate_limited`.
- State-changing calls accept an `Idempotency-Key` header; repeating a key returns the first result.
- All numbers are computed by the server. The client formats, it never calculates.

## Session and account

| Call | Body | Returns |
|---|---|---|
| `POST /api/session` | – | `Me`. Creates an anonymous visitor if there is no valid cookie. Safe to call on every page load. |
| `GET /api/me` | – | `Me` |
| `POST /api/auth/link` | `{email}` | `{sent: true}`. Emails a one-time sign-in link (in demo/dev it lands in the inbox, see below). |
| `POST /api/auth/verify` | `{token}` | `Me` and a new cookie |
| `POST /api/auth/logout` | – | `{}` |

```
Me = {
  user: {id, anonymous: bool, email: string|null, demo: bool},
  subscription: Subscription|null,
  entitled: bool,                 // true for active subscribers and for demo sessions
  today: "2026-11-20",            // the user's current date (demo clock applied)
  demo: {persona: "maya", started_on, today, real_today} | null,
  config: {billing_provider: "demo"|"stripe", test_mode: bool, test_card: "4242 4242 4242 4242",
           llm_provider: "openai"|"anthropic"|"fake", sms_live: bool}
}
```

## Demo mode (for guests and judges)

| Call | Body | Returns |
|---|---|---|
| `POST /api/demo/start` | `{persona?: "maya"}` | `Me`. Creates a demo account with sample cards, history and an active plan. The story starts on a payday. |
| `POST /api/demo/clock` | `{action: "next_payday"\|"next_statement"\|"plus_day"\|"reset"}` | `{me: Me, jumped_to: date, note: string}` |
| `GET /api/inbox` | – | `{messages: [{id, channel: "sms"\|"email", to, subject, body, link, sent_at}]}` newest first. Demo and dev only: what would have been sent. |

## Quiz (before payment)

| Call | Body | Returns |
|---|---|---|
| `GET /api/quiz` | – | `Quiz` |
| `PUT /api/quiz/answers` | `{answers: {key: value}}` merged into the stored answers | `Quiz` |
| `PUT /api/quiz/cards` | `{cards: [CardInput]}` 1–6 cards, replaces the list | `Quiz` |
| `PUT /api/quiz/schedule` | `ScheduleInput` | `Quiz` |
| `POST /api/quiz/sample` | – | `Quiz` filled with the sample persona's numbers |
| `POST /api/quiz/card-burn` | `{balance_cents, apr_bps}` | `{burn_per_day, per_year_cents}` live line while typing |
| `POST /api/quiz/result` | – | `Result`. Needs at least one card and a schedule. The result is stored and does not change after payment. |
| `GET /api/quiz/result` | – | `Result` or 404 |

```
CardInput     = {name, issuer?, balance_cents, apr_bps, statement_min_cents?, due_day (1-28), close_day? (1-31)}
ScheduleInput = {frequency: "weekly"|"biweekly"|"semimonthly"|"monthly", anchor: date (next payday), per_payday_cents, second_day?}
Quiz   = {answers: {...}, cards: [CardInput], schedule: ScheduleInput|null, has_result: bool}
Result = {
  id, created_at, engine_version, today,
  burn_per_day, per_paycheck_cents, paycheck_days, per_month_cents, per_year_cents, total_balance_cents,
  cards: [{id, name, issuer, balance_cents, apr_bps, burn_per_day, min_payment_cents, next_due}],   // highest burn first
  first_move: {payday, amount_cents, card_id, card_name, covers_min, min_cents, extra_cents,
               saved_by_date_cents, days_early, burn_after, strategy, reason, short_by_cents} | null,
  payoff: {monthly_budget_cents, plan: Projection, minimums: Projection},
  point_value: {card_id, card_name, per_point_per_year_cents},
  asks: [{type, title, detail, value_label}],
  assumptions: [string]
}
Projection = {strategy, months, interest_cents, never: bool, debt_free_month: "YYYY-MM"|null}
```
Quiz answer keys used by the server: `late_fee_recently` (bool), `late_fee_card` (card name), `late_fee_cents`,
`annual_fee_card`, `autopay` ("min"|"none"|"full"|"unsure"), `age_band`, `carry_months`. Other keys are stored as given.

## Billing

| Call | Body | Returns |
|---|---|---|
| `GET /api/billing/plans` | – | `{plans: [Plan], guarantee_days: 14, provider, test_mode, test_card}` |
| `POST /api/billing/checkout` | `{plan_id, email, consent_renewal: true}` | `Checkout`. `consent_renewal` must be true (the separate tick box). |
| `POST /api/billing/checkout/{id}/pay` | `{card_number, exp, cvc, name?}` | `Me`. Test provider only: accepts the test card shown on screen. |
| `GET /api/billing/subscription` | – | `Subscription` or 404 |
| `POST /api/billing/cancel` | `{reason?}` | `Subscription` (access stays until the period ends) |
| `POST /api/billing/resume` | – | `Subscription` |
| `POST /api/billing/pause` | `{weeks: 1-8}` | `Subscription` |
| `POST /api/billing/refund` | – | `Subscription`. Unconditional inside the guarantee window. |

```
Plan = {id: "w1"|"w4"|"w12", name, price_cents, days, renews_price_cents, renews_days, per_day_cents (float), then: "then $79.99 every 12 weeks", tag: string|null}
Checkout = {id, provider, url: string|null /* stripe: redirect here */, plan: Plan, amount_cents, test_mode, test_card, status}
Subscription = {status: "active"|"canceled"|"paused"|"past_due"|"refunded", plan: Plan, started_at, current_period_end,
                cancel_at_period_end: bool, paused_until: date|null, renews_price_cents, paid_total_cents, refundable_until: date|null}
```

## Product (needs `entitled`)

### Today
`GET /api/today` returns everything the home screen shows.
```
Today = {
  date, headline: "It's payday." | "Done for this payday." | "Next payday is Friday, Dec 4.",
  lead: string,
  instruction: Instruction | null,
  burn: {now, start, down_by, started_on, history: [{date, value, label, delta}]},     // values are cents per day
  autopay: [{card_id, card_name, issuer, min_cents, due, source: "this_paycheck"|"next_paycheck"|"covered_by_payment", note}],
  next_ask: Ask | null,
  sprint: {done, total, ends_on} | null,
  meter: Meter,
  cards: [CardView],
  upcoming: [{date, kind: "payday"|"statement_close"|"due", card_id?, title}]
}
Instruction = {
  id, payday, next_payday, status: "due"|"paid"|"short"|"upcoming",
  sends: [{card_id, card_name, issuer, last4, apr_bps, amount_cents, covers_min, min_cents, extra_cents, due, days_early,
           saved_by_date_cents, burn_cut, pay_url}],                                   // usually one line
  reserved_cents, short_by_cents, strategy, reason, burn_before, burn_after, total_send_cents,
  paid_at: timestamp|null, paid_cents: int|null
}
Meter = {paid_cents, confirmed_cents, estimated_cents, found_cents, items: [{label, cents, kind: "confirmed"|"estimated", date}]}
```
| Call | Body | Returns |
|---|---|---|
| `POST /api/plan/instructions/{id}/paid` | `{amount_cents?}` (defaults to the instruction's amount) | `Today` |
| `POST /api/plan/instructions/{id}/undo` | – | `Today` |
| `POST /api/plan/instructions/{id}/short` | `{available_cents}` | `Today` with a recalculated instruction |
| `GET /api/plan/schedule` / `PUT /api/plan/schedule` | `ScheduleInput` | `ScheduleInput` |
| `GET /api/plan/projection` | – | `{monthly_budget_cents, avalanche: Projection, snowball: Projection, minimums: Projection}` |

### Cards
| Call | Body | Returns |
|---|---|---|
| `GET /api/cards` | – | `{cards: [CardView], burn_per_day, total_balance_cents}` |
| `POST /api/cards` | `CardInput` | `CardView` |
| `PATCH /api/cards/{id}` | any of `{name, last4, due_day, close_day, autopay, statement_min_cents}` | `CardView` |
| `DELETE /api/cards/{id}` | – | `{ok: true}` |
| `POST /api/cards/{id}/charges` | `{amount_cents, date?, note?}` a new purchase landed | `CardView` |
| `GET /api/cards/{id}/ledger` | – | `{events: [{id, type, amount_cents, signed_cents, date, source, note}]}` |
| `GET /api/cards/{id}/refinance?monthly_payment_cents=` | – | `Refinance` |

```
CardView = {id, name, issuer, last4, balance_cents, apr_bps, burn_per_day, min_payment_cents, due_day, close_day,
            next_due, autopay, pay_url, start_balance_cents, progress (0..1), paid_off: bool, min_rule_verified: bool}
Refinance = {stay: {months, interest_cents},
             transfer: {promo_months, fee_3: T, fee_5: T},   // T = {fee_cents, breakeven_months, transfer_cost_cents, transfer_months, saving_cents, clears_in_promo, payment_to_clear_in_promo_cents}
             loans: [{loan_apr_bps, origination_bps, payment_cents, loan_cost_cents, cards_cost_cents, saving_cents}],
             note}
```

### Statements
| Call | Body | Returns |
|---|---|---|
| `POST /api/statements` | multipart: `card_id`, and `file` (PDF, PNG, JPG up to 10 MB) or `text` | `Statement` with `status: "extracting"` |
| `POST /api/statements/manual` | `{card_id, fields: StatementFields}` | `Statement` |
| `GET /api/statements` | `?card_id=` | `{statements: [Statement]}` newest first |
| `GET /api/statements/{id}` | – | `Statement`. Poll every second while `status` is `extracting`. |
| `POST /api/statements/{id}/confirm` | `{fields: StatementFields}` corrected by the user | `Statement` |

```
StatementFields = {new_balance_cents, previous_balance_cents, payments_credits_cents, purchases_cents, fees_cents, interest_cents,
                   apr_bps, balance_subject_to_interest_cents, days_in_cycle, min_payment_cents, close_date, due_date,
                   late_fee_cents, interest_ytd_cents}            // every field optional
Statement = {
  id, card_id, card_name, status: "extracting"|"needs_confirmation"|"reconciled"|"failed", source: "upload"|"manual"|"demo",
  fields: StatementFields, checks: [{name, ok: bool|null, detail}], confirm_fields: [field names to highlight],
  reconciliation: {predicted_interest_cents, actual_interest_cents, diff_cents, parts: [{label, cents}], summary, within_tolerance} | null,
  triggers: [{type, title, detail}],        // new reasons to ask the issuer for something
  error: string|null, created_at, close_date, due_date
}
```

### Asks (the two-week sprint)
| Call | Body | Returns |
|---|---|---|
| `GET /api/asks` | – | `{asks: [Ask], sprint: {done, total, ends_on}}` in queue order |
| `GET /api/asks/{id}` | – | `AskDetail` |
| `POST /api/asks/{id}/outcome` | `Outcome` | `{ask: Ask, today: Today}` |
| `POST /api/asks/{id}/skip` | – | `Ask` |
| `GET /api/playbook?issuer=&type=` | – | `{entries: [...], count}` what other users reported |

```
Ask = {id, type: "late_fee_refund"|"apr_reduction"|"annual_fee"|"due_date_move"|"hardship", card_id, card_name, issuer,
       title, subtitle, value_cents: int|null, value_label, status: "queued"|"done"|"skipped", order, result: string|null}
AskDetail = Ask & {
  why: string, evidence: {text, source, caveat},
  script: {opening, lines: [{label, text}], must_ask: [string], if_refused: string},
  written: string,                                  // the same ask as a chat or secure-message text
  call_card: {start: nodeId, nodes: {[nodeId]: {say: string, hint?: string, options: [{label, next: nodeId}], outcome?: string}}},
  playbook: {count, entries: [{channel, result, note, month}]}
}
Outcome = {result: "granted"|"partial"|"temporary"|"refused"|"no_answer", channel: "phone"|"chat"|"message",
           new_apr_bps?, until?: date, refund_cents?, rep_name?, hard_pull?: "yes"|"no"|"unknown", ask_again_on?: date, note?}
```

### Rehearsal with an AI playing the bank rep
| Call | Body | Returns |
|---|---|---|
| `POST /api/rehearsal` | `{ask_id}` | `Rehearsal` with the rep's opening line |
| `POST /api/rehearsal/{id}/turn` | `{text}` | `text/event-stream`: `event: delta` `data: {"text": "..."}` repeated, then `event: done` `data: Rehearsal` |
| `GET /api/rehearsal/{id}` | – | `Rehearsal` |

```
Rehearsal = {id, ask_id, scenario, provider, finished: bool,
             messages: [{role: "rep"|"you", text}],
             checklist: [{id, label, done: bool}]}       // the must-ask questions, ticked as the user asks them
```

### Other
| Call | Body | Returns |
|---|---|---|
| `GET /api/meter` | – | `Meter` ("paid for itself") |
| `GET /api/notifications/prefs` / `PUT` | `{phone, sms_opt_in, email_opt_in, timezone}` | prefs |
| `GET /api/calendar.ics?u=&sig=` | – | calendar file with paydays, closing dates and due dates (link comes from prefs: `calendar_url`) |
| `POST /api/events` | `{events: [{name, props?, ts?}]}` | 202. Product analytics; never send amounts typed by the user as free text. |
| `GET /api/health` | – | `{ok, version, engine_version}` |
