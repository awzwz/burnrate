// Route table of the in-browser mock. Mirrors docs/API.md: same paths, same shapes, same error envelope.
import type {
  Ask,
  AskDetail,
  AutopayRow,
  CardInput,
  CardView,
  Instruction,
  Me,
  Outcome,
  Quiz,
  Rehearsal,
  Result,
  ScheduleInput,
  Statement,
  Today,
  Upcoming,
} from "../api/types";
import { dateLong, dateShort, dateWeekday, pct, usd, usdShort } from "../lib/format";
import type { MockAsk } from "./content";
import {
  addDays,
  buildInstruction,
  burnOf,
  guessIssuer,
  issuerName,
  minPayment,
  nextDayOfMonth,
  PAY_URLS,
  PAYCHECK_DAYS,
  paydays,
  PAYDAYS_PER_YEAR,
  projectMinimums,
  projectPlan,
  realToday,
  refinance,
  round4,
  shortName,
  totalBurn,
} from "./engine";
import type { Account, DB, MCard, MRehearsal, MStatement } from "./state";
import {
  account,
  advanceTo,
  GUARANTEE_DAYS,
  liveCards,
  load,
  makeCard,
  meter,
  newUpload,
  nextId,
  PLANS,
  pushHistory,
  reconcile,
  recordEvent,
  refreshInstruction,
  runChecks,
  save,
  seedDemo,
  seedFromQuiz,
  settle,
  stamp,
  TEST_CARD,
} from "./state";

class HttpError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}
const invalid = (message: string) => new HttpError(422, "invalid_input", message);
const notFound = (message = "Not found.") => new HttpError(404, "not_found", message);

interface Req {
  method: string;
  path: string;
  query: URLSearchParams;
  body: Record<string, unknown>;
  form: FormData | null;
}

// ---------- views ----------
function entitled(acc: Account | null): boolean {
  if (!acc) return false;
  if (acc.demo) return true;
  const s = acc.subscription;
  if (s.status === "active") return true;
  return s.status === "canceled" && s.current_period_end > new Date().toISOString();
}

function me(d: DB): Me {
  const acc = account(d);
  return {
    user: { id: d.user_id, anonymous: !acc, email: acc ? acc.email : d.email, demo: !!acc?.demo },
    subscription: acc ? acc.subscription : null,
    entitled: entitled(acc),
    today: acc ? acc.today : realToday(),
    demo: acc?.demo ? { persona: "maya", started_on: acc.story_start, today: acc.today, real_today: acc.real_today } : null,
    config: { billing_provider: "demo", test_mode: true, test_card: TEST_CARD, llm_provider: "fake", sms_live: false },
  };
}

function cardView(c: MCard, on: string): CardView {
  const start = Math.max(c.start_balance_cents, 1);
  return {
    id: c.id,
    name: c.name,
    issuer: c.issuer,
    last4: c.last4,
    balance_cents: c.balance_cents,
    apr_bps: c.apr_bps,
    burn_per_day: round4(burnOf(c)),
    min_payment_cents: minPayment(c),
    due_day: c.due_day,
    close_day: c.close_day,
    next_due: nextDayOfMonth(c.due_day, on),
    autopay: c.autopay,
    pay_url: PAY_URLS[c.issuer] ?? "",
    start_balance_cents: c.start_balance_cents,
    progress: Math.min(1, Math.max(0, (start - c.balance_cents) / start)),
    paid_off: c.balance_cents <= 0,
    min_rule_verified: c.issuer === "chase" || c.issuer === "discover" || c.statement_min_cents !== null,
  };
}

const askView = (a: MockAsk): Ask => ({
  id: a.id, type: a.type, card_id: a.card_id, card_name: a.card_name, issuer: a.issuer, title: a.title, subtitle: a.subtitle,
  value_cents: a.value_cents, value_label: a.value_label, status: a.status, order: a.order, result: a.result,
});
const askDetail = (a: MockAsk): AskDetail => ({
  ...askView(a), why: a.why, evidence: a.evidence, script: a.script, written: a.written, call_card: a.call_card, playbook: a.playbook,
});
function sprint(acc: Account) {
  if (!acc.asks.length) return null;
  return { done: acc.asks.filter((a) => a.status !== "queued").length, total: acc.asks.length, ends_on: acc.sprint_ends };
}

function todayView(acc: Account): Today {
  refreshInstruction(acc);
  const cards = liveCards(acc);
  const ins = acc.instruction;
  const now = totalBurn(cards);
  const send = ins?.sends[0];
  const autopay: AutopayRow[] = cards
    .filter((c) => c.balance_cents > 0)
    .map((c) => {
      const due = nextDayOfMonth(c.due_day, acc.today);
      const min = minPayment(c);
      let source: AutopayRow["source"] = "next_paycheck";
      let note = "paid by autopay";
      if (ins) {
        // The paycheck that sets this minimum aside is the last one before its due date.
        const upcomingPayday = ins.status === "upcoming" ? ins.payday : ins.next_payday;
        const before = paydays(acc.schedule, upcomingPayday, 8).filter((p) => p < due);
        note = `from your ${dateShort(before.length ? before[before.length - 1] : upcomingPayday)} paycheck`;
        if (send && send.card_id === c.id && send.covers_min && send.due === due) {
          source = "covered_by_payment";
          note = ins.status === "paid" ? "covered by your payment" : ins.status === "upcoming" ? "covered by your next payment" : ins.payday === acc.today ? "covered by today's payment" : "covered by this payment";
        } else if (due < upcomingPayday) {
          source = "this_paycheck";
          note = ins.status === "upcoming" ? "set aside from your last paycheck" : "reserved from this paycheck";
        }
      }
      return { card_id: c.id, card_name: c.name, issuer: c.issuer, min_cents: min, due, source, note };
    })
    .sort((a, b) => (a.due < b.due ? -1 : 1));

  const upcoming: Upcoming[] = [];
  for (const p of paydays(acc.schedule, addDays(acc.today, 1), 2)) upcoming.push({ date: p, kind: "payday", title: "Payday" });
  for (const c of cards) {
    upcoming.push({ date: nextDayOfMonth(c.close_day, addDays(acc.today, 1)), kind: "statement_close", card_id: c.id, title: `${c.name} statement closes` });
    upcoming.push({ date: nextDayOfMonth(c.due_day, acc.today), kind: "due", card_id: c.id, title: `${c.name} payment due` });
  }
  upcoming.sort((a, b) => (a.date < b.date ? -1 : 1));

  let headline = "You're all set.";
  let lead = "No balance is collecting interest right now.";
  if (ins) {
    const nextClose = upcoming.find((u) => u.kind === "statement_close");
    if (ins.status === "paid") {
      headline = "Done for this payday.";
      lead = nextClose ? `Next move: your ${shortName(cards.find((c) => c.id === nextClose.card_id) ?? { issuer: "", name: "card" })} statement closes ${dateShort(nextClose.date)}.` : "Nothing else to do until your next payday.";
    } else if (ins.status === "upcoming") {
      headline = `Next payday is ${dateWeekday(ins.payday)}.`;
      lead = "Nothing to send today. Autopay keeps every minimum on time.";
    } else if (ins.payday === acc.today) {
      headline = "It's payday.";
      lead = ins.status === "short" ? "A smaller payment this time. Nothing is marked as missed." : "One payment today. Autopay handles the rest.";
    } else {
      headline = `Your ${dateShort(ins.payday)} payday plan is open.`;
      lead = "One payment whenever you're ready. Autopay handles the rest.";
    }
  }
  const next = acc.asks.find((a) => a.status === "queued");
  return {
    date: acc.today,
    headline,
    lead,
    instruction: ins,
    burn: { now: round4(now), start: acc.burn_start, down_by: round4(acc.burn_start - now), started_on: acc.started_on, history: acc.history },
    autopay,
    next_ask: next ? askView(next) : null,
    sprint: sprint(acc),
    meter: meter(acc),
    cards: cards.map((c) => cardView(c, acc.today)),
    upcoming: upcoming.slice(0, 8),
  };
}

function statementView(st: MStatement): Statement {
  const { ready_at: _r, resolve_to: _t, ...rest } = st;
  void _r;
  void _t;
  return rest;
}

function rehearsalView(r: MRehearsal): Rehearsal {
  const { turns: _turns, ...rest } = r;
  void _turns;
  return rest;
}

// ---------- quiz ----------
const quizView = (d: DB): Quiz => ({ answers: d.quiz.answers, cards: d.quiz.cards, schedule: d.quiz.schedule, has_result: !!d.quiz.result });

function validCard(c: Partial<CardInput>, n: number): CardInput {
  const label = n ? `Card ${n}: ` : "";
  if (!c.name || !String(c.name).trim()) throw invalid(`${label}give the card a name.`);
  if (String(c.name).length > 60) throw invalid(`${label}the name is too long (60 characters at most).`);
  if (!Number.isInteger(c.balance_cents) || (c.balance_cents as number) < 0 || (c.balance_cents as number) > 50_000_000) throw invalid(`${label}the balance must be between $0 and $500,000.`);
  if (!Number.isInteger(c.apr_bps) || (c.apr_bps as number) < 0 || (c.apr_bps as number) > 7999) throw invalid(`${label}the APR must be between 0% and 79.99%.`);
  if (!Number.isInteger(c.due_day) || (c.due_day as number) < 1 || (c.due_day as number) > 28) throw invalid(`${label}the due day must be between 1 and 28.`);
  if (c.statement_min_cents != null && (!Number.isInteger(c.statement_min_cents) || c.statement_min_cents < 0)) throw invalid(`${label}the minimum payment is not an amount.`);
  return {
    name: String(c.name).trim(),
    issuer: c.issuer && c.issuer !== "other" ? c.issuer : guessIssuer(String(c.name)),
    balance_cents: c.balance_cents as number,
    apr_bps: c.apr_bps as number,
    statement_min_cents: c.statement_min_cents ?? null,
    due_day: c.due_day as number,
    close_day: c.close_day,
  };
}

function validSchedule(b: Record<string, unknown>): ScheduleInput {
  const f = b.frequency;
  if (f !== "weekly" && f !== "biweekly" && f !== "semimonthly" && f !== "monthly") throw invalid("Choose how often you are paid.");
  if (typeof b.anchor !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(b.anchor)) throw invalid("Enter your next payday as a date.");
  if (!Number.isInteger(b.per_payday_cents) || (b.per_payday_cents as number) < 0 || (b.per_payday_cents as number) > 5_000_000) throw invalid("The amount per paycheck must be between $0 and $50,000.");
  const second = b.second_day === undefined ? 15 : Number(b.second_day);
  if (!Number.isInteger(second) || second < 1 || second > 28) throw invalid("The second payday must be a day between 1 and 28.");
  return { frequency: f, anchor: b.anchor, per_payday_cents: b.per_payday_cents as number, second_day: second };
}

function makeResult(d: DB): Result {
  const q = d.quiz;
  if (!q.cards.length) throw new HttpError(400, "no_cards", "Add at least one card first.");
  if (!q.schedule) throw new HttpError(400, "no_schedule", "Tell us when you get paid first.");
  const today = realToday();
  const cards = q.cards.map((c, i) => ({
    id: c.id ?? `c${i + 1}`,
    name: c.name,
    issuer: c.issuer ?? guessIssuer(c.name),
    balance_cents: c.balance_cents,
    apr_bps: c.apr_bps,
    due_day: c.due_day,
    close_day: c.close_day ?? 1,
    statement_min_cents: c.statement_min_cents ?? null,
  }));
  const burn = totalBurn(cards);
  const payday = paydays(q.schedule, today, 1)[0];
  const ins = buildInstruction(cards, q.schedule, payday);
  const send = ins?.sends[0];
  const budget = Math.round((q.schedule.per_payday_cents * PAYDAYS_PER_YEAR[q.schedule.frequency]) / 12);
  const biggest = [...cards].sort((a, b) => b.balance_cents - a.balance_cents);
  const asks: Result["asks"] = [];
  if (q.answers.late_fee_recently) {
    const fee = Number(q.answers.late_fee_cents) || 0;
    asks.push({ type: "late_fee_refund", title: `Ask for a late fee back: ${q.answers.late_fee_card || "your card"}`, detail: "Issuers often waive a first late fee when asked.", value_label: fee ? usd(fee) : "the fee" });
  }
  for (const c of biggest.slice(0, 2)) {
    asks.push({ type: "apr_reduction", title: `Ask for a lower rate: ${c.name}`, detail: `Each point off is worth ${usdShort(Math.round(c.balance_cents / 10000) * 100)} a year on this balance.`, value_label: pct(c.apr_bps) });
  }
  const days = PAYCHECK_DAYS[q.schedule.frequency];
  return {
    id: nextId("res", d),
    created_at: new Date().toISOString(),
    engine_version: "mock-1",
    today,
    burn_per_day: round4(burn),
    per_paycheck_cents: Math.round(burn * days),
    paycheck_days: days,
    per_month_cents: Math.round((burn * 365) / 12),
    per_year_cents: Math.round(burn * 365),
    total_balance_cents: cards.reduce((s, c) => s + c.balance_cents, 0),
    cards: cards
      .map((c) => ({ id: c.id, name: c.name, issuer: c.issuer, balance_cents: c.balance_cents, apr_bps: c.apr_bps, burn_per_day: round4(burnOf(c)), min_payment_cents: minPayment(c), next_due: nextDayOfMonth(c.due_day, today) }))
      .sort((a, b) => b.burn_per_day - a.burn_per_day),
    first_move: ins && send
      ? {
          payday,
          amount_cents: send.amount_cents,
          card_id: send.card_id,
          card_name: send.card_name,
          covers_min: send.covers_min,
          min_cents: send.min_cents,
          extra_cents: send.extra_cents,
          saved_by_date_cents: send.saved_by_date_cents,
          days_early: send.days_early,
          burn_after: ins.burn_after,
          strategy: ins.strategy,
          reason: ins.reason,
          short_by_cents: ins.short_by_cents,
        }
      : null,
    payoff: { monthly_budget_cents: budget, plan: projectPlan(cards, budget, today), minimums: projectMinimums(cards, today) },
    point_value: { card_id: biggest[0].id, card_name: biggest[0].name, per_point_per_year_cents: Math.round(biggest[0].balance_cents / 100) },
    asks,
    assumptions: [
      "Balances and rates are the ones you entered. Your statement has the final figure.",
      "Interest is counted by the day: balance × APR ÷ 365.",
      "No new purchases on these cards, and rates stay where they are today.",
      "Minimum payments follow each issuer's published formula until a statement confirms them.",
      "The payoff comparison uses a monthly approximation of your paychecks.",
    ],
  };
}

// ---------- helpers for product routes ----------
function need(d: DB): Account {
  const acc = account(d);
  if (!acc || !entitled(acc)) throw new HttpError(402, "subscription_required", "This part of Burn Rate needs an active plan.");
  return acc;
}
function card(acc: Account, id: string): MCard {
  const c = liveCards(acc).find((x) => x.id === id);
  if (!c) throw notFound("We couldn't find that card.");
  return c;
}
function ask(acc: Account, id: string): MockAsk {
  const a = acc.asks.find((x) => x.id === id);
  if (!a) throw notFound("We couldn't find that ask.");
  return a;
}
function intField(b: Record<string, unknown>, key: string, label: string, min = 0, max = 50_000_000): number {
  const v = b[key];
  if (!Number.isInteger(v) || (v as number) < min || (v as number) > max) throw invalid(`${label} is not a valid amount.`);
  return v as number;
}

function applyOutcome(acc: Account, a: MockAsk, o: Outcome): void {
  const c = acc.cards.find((x) => x.id === a.card_id);
  const bank = issuerName({ issuer: a.issuer, name: a.card_name });
  a.status = "done";
  a.result = o.result;
  const good = o.result === "granted" || o.result === "partial" || o.result === "temporary";
  if (!c) return;
  const before = totalBurn(liveCards(acc));
  if (a.type === "apr_reduction" && good && typeof o.new_apr_bps === "number" && o.new_apr_bps < c.apr_bps) {
    const from = c.apr_bps;
    c.apr_bps = o.new_apr_bps;
    const label = `${bank} lowered your rate ${pct(from)} → ${pct(c.apr_bps)}`;
    recordEvent(acc, c, "rate_change", 0, "ask", label + (o.until ? ` until ${dateShort(o.until)}` : ""));
    pushHistory(acc, `${bank} lowered your rate to ${pct(c.apr_bps)}`, before);
    acc.rate_wins.push({ card_id: c.id, from_bps: from, to_bps: c.apr_bps, date: acc.today, until: o.until ?? null, label });
    a.subtitle = label;
    a.value_label = pct(c.apr_bps);
  } else if ((a.type === "late_fee_refund" || a.type === "annual_fee") && good) {
    const refund = Math.min(o.refund_cents ?? a.value_cents ?? 0, c.balance_cents);
    if (refund > 0) {
      c.balance_cents -= refund;
      recordEvent(acc, c, "credit", refund, "ask", `${bank} refunded a fee`);
      pushHistory(acc, `${bank} refunded ${usd(refund)}`, before);
      acc.meter_items.push({ label: `${bank} refunded a fee`, cents: refund, kind: "confirmed", date: acc.today });
      a.subtitle = `${bank} refunded ${usd(refund)}`;
    }
  } else if (a.type === "due_date_move" && good) {
    a.subtitle = "Due date change logged";
  } else if (o.result === "refused") {
    a.subtitle = o.ask_again_on ? `${bank} said no. Ask again ${dateShort(o.ask_again_on)}` : `${bank} said no this time`;
  } else if (o.result === "no_answer") {
    a.subtitle = "No answer yet";
  }
  acc.inbox.unshift({
    id: nextId("msg", acc), channel: "email", to: acc.email ?? "you@example.com",
    subject: `Logged: ${a.title}`, body: `Result: ${o.result.replace("_", " ")}. ${good ? "The next statement should show it; we will check it for you." : "Your payday plan does not change."}`,
    link: `/app/asks/${a.id}`, sent_at: stamp(acc.today, 11),
  });
  refreshInstruction(acc);
}

function repReply(a: MockAsk, r: MRehearsal, text: string): string {
  const low = text.toLowerCase();
  const answers: string[] = [];
  const pending = r.checklist.filter((c) => !c.done);
  for (const item of pending) {
    const m = a.must.find((x) => x.id === item.id);
    if (m && new RegExp(m.match, "i").test(low)) {
      item.done = true;
      answers.push(m.answer);
    }
  }
  r.turns += 1;
  const beat = a.rep.beats[r.turns - 1];
  const allAsked = r.checklist.every((c) => c.done);
  if (beat) return [...answers, beat].join(" ");
  if (allAsked || r.turns >= a.rep.beats.length + 3) {
    r.finished = true;
    return [...answers, a.rep.closing].join(" ");
  }
  if (answers.length) return [...answers, "Is there anything else you'd like to ask?"].join(" ");
  return "Sure. Is there anything else you'd like to ask before we finish?";
}

function sse(pieces: string[], finish: () => unknown): Response {
  const enc = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (i < pieces.length) {
        await new Promise((r) => setTimeout(r, 28));
        controller.enqueue(enc.encode(`event: delta\ndata: ${JSON.stringify({ text: pieces[i] })}\n\n`));
        i += 1;
        return;
      }
      controller.enqueue(enc.encode(`event: done\ndata: ${JSON.stringify(finish())}\n\n`));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function calendarUrl(acc: Account): string {
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Burn Rate//Mock//EN"];
  const add = (date: string, title: string) => {
    const d = date.replace(/-/g, "");
    lines.push("BEGIN:VEVENT", `UID:${d}-${title.replace(/\W+/g, "")}@burnrate`, `DTSTART;VALUE=DATE:${d}`, `SUMMARY:${title}`, "END:VEVENT");
  };
  for (const p of paydays(acc.schedule, acc.today, 6)) add(p, "Payday: open Burn Rate");
  for (const c of liveCards(acc)) {
    add(nextDayOfMonth(c.close_day, acc.today), `${c.name} statement closes`);
    add(nextDayOfMonth(c.due_day, acc.today), `${c.name} payment due`);
  }
  lines.push("END:VCALENDAR");
  return URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/calendar" }));
}

// ---------- router ----------
type Result_ = unknown | Response;

function route(d: DB, r: Req): Result_ {
  const { method, path, body } = r;
  const seg = path.split("/").filter(Boolean);
  const is = (m: string, p: string) => method === m && path === p;

  // session and account
  if (is("POST", "/session") || is("GET", "/me")) return me(d);
  if (is("POST", "/auth/link")) {
    const email = String(body.email ?? "").trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) throw invalid("Enter a valid email address.");
    const token = nextId("tok", d);
    d.login_tokens[token] = email;
    d.dev_inbox.unshift({ id: nextId("msg", d), channel: "email", to: email, subject: "Your Burn Rate sign-in link", body: "Open this link to sign in. It works once and expires in 15 minutes.", link: `/auth/verify?token=${token}`, sent_at: new Date().toISOString() });
    return { sent: true };
  }
  if (is("POST", "/auth/verify")) {
    const email = d.login_tokens[String(body.token ?? "")];
    if (!email) throw new HttpError(401, "invalid_token", "This sign-in link has expired or was already used. Ask for a new one.");
    delete d.login_tokens[String(body.token)];
    d.email = email;
    d.mode = d.paid && d.paid.email?.toLowerCase() === email.toLowerCase() ? "paid" : "anon";
    return me(d);
  }
  if (is("POST", "/auth/logout")) {
    if (d.mode === "demo") d.demo = null;
    d.mode = "anon";
    d.email = null;
    return {};
  }

  // demo
  if (is("POST", "/demo/start")) {
    d.demo = seedDemo();
    d.mode = "demo";
    return me(d);
  }
  if (is("POST", "/demo/clock")) {
    const acc = account(d);
    if (!acc || !acc.demo) throw new HttpError(409, "conflict", "The clock only moves in a demo session.");
    const action = body.action;
    let notes: string[] = [];
    if (action === "reset") {
      d.demo = seedDemo();
      return { me: me(d), jumped_to: d.demo.today, note: "Back to the start of the story: a payday." };
    }
    if (action === "plus_day") notes = advanceTo(acc, addDays(acc.today, 1));
    else if (action === "next_payday") notes = advanceTo(acc, paydays(acc.schedule, addDays(acc.today, 1), 1)[0]);
    else if (action === "next_statement") {
      const closes = liveCards(acc).map((c) => nextDayOfMonth(c.close_day, addDays(acc.today, 1))).sort();
      notes = advanceTo(acc, closes[0] ?? addDays(acc.today, 1));
    } else throw invalid("Unknown clock action.");
    return { me: me(d), jumped_to: acc.today, note: [`Jumped to ${dateLong(acc.today)}.`, ...notes.filter((n) => !n.endsWith("is a payday."))].join(" ") };
  }
  if (is("GET", "/inbox")) {
    const acc = account(d);
    return { messages: [...(acc ? acc.inbox : []), ...d.dev_inbox].sort((a, b) => (a.sent_at < b.sent_at ? 1 : -1)) };
  }

  // quiz
  if (is("GET", "/quiz")) return quizView(d);
  if (is("PUT", "/quiz/answers")) {
    const answers = body.answers;
    if (!answers || typeof answers !== "object") throw invalid("Send the answers as an object.");
    d.quiz.answers = { ...d.quiz.answers, ...(answers as Record<string, unknown>) };
    return quizView(d);
  }
  if (is("PUT", "/quiz/cards")) {
    const list = body.cards;
    if (!Array.isArray(list) || list.length < 1 || list.length > 6) throw invalid("Add between one and six cards.");
    d.quiz.cards = list.map((c, i) => ({ ...validCard(c as Partial<CardInput>, list.length > 1 ? i + 1 : 0), id: `c${i + 1}` }));
    return quizView(d);
  }
  if (is("PUT", "/quiz/schedule")) {
    d.quiz.schedule = validSchedule(body);
    return quizView(d);
  }
  if (is("POST", "/quiz/sample")) {
    const sample = seedDemo();
    d.quiz.cards = sample.cards.map((c) => ({ id: c.id, name: c.name, issuer: c.issuer, balance_cents: c.balance_cents, apr_bps: c.apr_bps, statement_min_cents: c.statement_min_cents, due_day: c.due_day, close_day: c.close_day }));
    d.quiz.schedule = sample.schedule;
    d.quiz.answers = { ...d.quiz.answers, card_count: "3", age_band: "25-34", carry_months: "over_12", late_fee_recently: true, late_fee_card: "Chase Freedom Unlimited", late_fee_cents: 3900, autopay: "min", sample: true };
    return quizView(d);
  }
  if (is("POST", "/quiz/card-burn")) {
    const balance = intField(body, "balance_cents", "The balance");
    const apr = intField(body, "apr_bps", "The APR", 0, 7999);
    const burn = burnOf({ balance_cents: balance, apr_bps: apr });
    return { burn_per_day: round4(burn), per_year_cents: Math.round(burn * 365) };
  }
  if (is("POST", "/quiz/result")) {
    d.quiz.result = makeResult(d);
    return d.quiz.result;
  }
  if (is("GET", "/quiz/result")) {
    if (!d.quiz.result) throw notFound("No result yet.");
    return d.quiz.result;
  }

  // billing
  if (is("GET", "/billing/plans")) return { plans: PLANS, guarantee_days: GUARANTEE_DAYS, provider: "demo", test_mode: true, test_card: TEST_CARD };
  if (is("POST", "/billing/checkout")) {
    const plan = PLANS.find((p) => p.id === body.plan_id);
    if (!plan) throw invalid("Choose one of the plans.");
    const email = String(body.email ?? "").trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) throw invalid("Enter a valid email address for your receipt.");
    if (body.consent_renewal !== true) throw invalid("Please tick the box to confirm the renewal terms.");
    const id = nextId("co", d);
    d.checkouts[id] = { id, provider: "demo", url: null, plan, amount_cents: plan.price_cents, test_mode: true, test_card: TEST_CARD, status: "open", email };
    const { email: _e, ...view } = d.checkouts[id];
    void _e;
    return view;
  }
  if (seg[0] === "billing" && seg[1] === "checkout" && seg[2]) {
    const co = d.checkouts[seg[2]];
    if (!co) throw notFound("This checkout has expired. Please choose your plan again.");
    if (method === "POST" && seg[3] === "pay") {
      const number = String(body.card_number ?? "").replace(/\D/g, "");
      if (number !== TEST_CARD.replace(/\D/g, "")) throw new HttpError(422, "card_declined", `This is a test checkout. Use the test card ${TEST_CARD}.`);
      const m = /^(\d{2})\s*\/\s*(\d{2})$/.exec(String(body.exp ?? ""));
      const now = new Date();
      if (!m || Number(m[1]) < 1 || Number(m[1]) > 12 || 2000 + Number(m[2]) < now.getFullYear() || (2000 + Number(m[2]) === now.getFullYear() && Number(m[1]) < now.getMonth() + 1)) {
        throw invalid("Enter an expiry date in the future, as MM/YY.");
      }
      if (!/^\d{3,4}$/.test(String(body.cvc ?? ""))) throw invalid("Enter the 3 or 4 digit security code.");
      co.status = "paid";
      d.paid = seedFromQuiz(d, co.plan, co.email);
      d.email = co.email;
      d.mode = "paid";
      return me(d);
    }
  }
  if (seg[0] === "billing" && ["subscription", "cancel", "resume", "pause", "refund"].includes(seg[1] ?? "")) {
    const acc = account(d);
    if (!acc) throw notFound("No subscription yet.");
    const s = acc.subscription;
    if (is("GET", "/billing/subscription")) return s;
    if (is("POST", "/billing/cancel")) {
      s.cancel_at_period_end = true;
      s.paused_until = null;
      if (s.status === "paused") s.status = "active";
      return s;
    }
    if (is("POST", "/billing/resume")) {
      s.cancel_at_period_end = false;
      s.paused_until = null;
      if (s.status !== "refunded") s.status = "active";
      return s;
    }
    if (is("POST", "/billing/pause")) {
      const weeks = Number(body.weeks);
      if (!Number.isInteger(weeks) || weeks < 1 || weeks > 8) throw invalid("Choose a pause between 1 and 8 weeks.");
      s.status = "paused";
      s.cancel_at_period_end = false;
      s.paused_until = addDays(acc.today, weeks * 7);
      return s;
    }
    if (is("POST", "/billing/refund")) {
      if (!s.refundable_until || s.refundable_until < acc.today) throw new HttpError(409, "conflict", "The money-back window for this plan has ended.");
      s.status = "refunded";
      s.cancel_at_period_end = true;
      s.refundable_until = null;
      s.paid_total_cents = 0;
      return s;
    }
  }

  // other, no entitlement needed
  if (is("POST", "/events")) return new Response(null, { status: 202 });
  if (is("GET", "/health")) return { ok: true, version: "mock", engine_version: "mock-1" };

  // ---------- product ----------
  if (is("GET", "/notifications/prefs") || is("PUT", "/notifications/prefs")) {
    const acc = account(d);
    if (!acc) throw new HttpError(402, "subscription_required", "This part of Burn Rate needs an active plan.");
    if (method === "PUT") {
      const phone = body.phone ? String(body.phone).trim() : null;
      if (phone && phone.replace(/\D/g, "").length < 10) throw invalid("Enter a 10-digit US mobile number.");
      if (body.sms_opt_in === true && !phone) throw invalid("Add a mobile number before turning on text messages.");
      acc.prefs = { ...acc.prefs, phone, sms_opt_in: body.sms_opt_in === true, email_opt_in: body.email_opt_in !== false, timezone: String(body.timezone || acc.prefs.timezone) };
    }
    return { ...acc.prefs, calendar_url: calendarUrl(acc) };
  }

  const acc = need(d);
  if (is("GET", "/today")) return todayView(acc);
  if (is("GET", "/meter")) return meter(acc);
  if (is("GET", "/plan/schedule")) return acc.schedule;
  if (is("PUT", "/plan/schedule")) {
    acc.schedule = validSchedule(body);
    acc.instruction = acc.instruction?.status === "paid" ? acc.instruction : null;
    refreshInstruction(acc);
    return acc.schedule;
  }
  if (is("GET", "/plan/projection")) {
    const cards = liveCards(acc);
    const budget = Math.round((acc.schedule.per_payday_cents * PAYDAYS_PER_YEAR[acc.schedule.frequency]) / 12);
    const avalanche = projectPlan(cards, budget, acc.today);
    return { monthly_budget_cents: budget, avalanche, snowball: { ...avalanche, strategy: "snowball" }, minimums: projectMinimums(cards, acc.today) };
  }
  if (seg[0] === "plan" && seg[1] === "instructions" && seg[2] && method === "POST") {
    const ins = acc.instruction;
    if (!ins || ins.id !== seg[2]) throw notFound("This instruction is no longer current. Refresh to see today's plan.");
    const send = ins.sends[0];
    const c = card(acc, send.card_id);
    if (seg[3] === "paid") {
      if (ins.status === "paid") return todayView(acc);
      const amount = body.amount_cents === undefined ? ins.total_send_cents : intField(body, "amount_cents", "The amount", 1);
      const paidAmount = Math.min(amount, c.balance_cents);
      const before = totalBurn(liveCards(acc));
      c.balance_cents -= paidAmount;
      c.cycle_paid_cents += paidAmount;
      recordEvent(acc, c, "payment", paidAmount, "payday", `Payday payment, instruction ${ins.id}`);
      pushHistory(acc, `Payday, ${usdShort(paidAmount)} to ${shortName(c)}`, before);
      const saved = Math.round(((paidAmount * c.apr_bps) / 10000 / 365) * send.days_early);
      if (saved > 0) acc.meter_items.push({ label: `Paid on payday instead of the due date #${ins.id}`, cents: saved, kind: "estimated", date: acc.today });
      const paid: Instruction = { ...ins, status: "paid", paid_at: new Date().toISOString(), paid_cents: paidAmount };
      acc.instruction = paid;
      return todayView(acc);
    }
    if (seg[3] === "undo") {
      if (ins.status !== "paid" || ins.paid_cents === null) return todayView(acc);
      c.balance_cents += ins.paid_cents;
      c.cycle_paid_cents = Math.max(0, c.cycle_paid_cents - ins.paid_cents);
      const idx = acc.ledger.map((e) => e.note).lastIndexOf(`Payday payment, instruction ${ins.id}`);
      if (idx >= 0) acc.ledger.splice(idx, 1);
      for (let i = acc.history.length - 1; i >= 0; i--) {
        if (acc.history[i].label.startsWith("Payday,") && acc.history[i].date === acc.today) {
          acc.history.splice(i, 1);
          break;
        }
      }
      acc.meter_items = acc.meter_items.filter((m) => !m.label.endsWith(`#${ins.id}`));
      acc.instruction = { ...ins, status: "due", paid_at: null, paid_cents: null };
      refreshInstruction(acc);
      return todayView(acc);
    }
    if (seg[3] === "short") {
      if (ins.status === "paid") throw new HttpError(409, "conflict", "This payday is already marked as paid. Undo it first.");
      const available = intField(body, "available_cents", "The amount available", 0, 5_000_000);
      acc.short = { payday: ins.payday, available_cents: available };
      refreshInstruction(acc);
      return todayView(acc);
    }
  }

  // cards
  if (is("GET", "/cards")) {
    const cards = liveCards(acc);
    return { cards: cards.map((c) => cardView(c, acc.today)), burn_per_day: round4(totalBurn(cards)), total_balance_cents: cards.reduce((s, c) => s + c.balance_cents, 0) };
  }
  if (is("POST", "/cards")) {
    if (liveCards(acc).length >= 6) throw new HttpError(409, "conflict", "Burn Rate tracks up to six cards.");
    const input = validCard(body as Partial<CardInput>, 0);
    const before = totalBurn(liveCards(acc));
    const c = makeCard(acc, input, nextId("c", acc));
    acc.cards.push(c);
    recordEvent(acc, c, "opening", c.balance_cents, "manual", "Balance when you added the card");
    pushHistory(acc, `Added ${c.name}`, before);
    refreshInstruction(acc);
    return cardView(c, acc.today);
  }
  if (seg[0] === "cards" && seg[1]) {
    const c = card(acc, seg[1]);
    if (method === "PATCH" && !seg[2]) {
      if (body.name !== undefined) {
        if (!String(body.name).trim()) throw invalid("Give the card a name.");
        c.name = String(body.name).trim().slice(0, 60);
      }
      if (body.last4 !== undefined) c.last4 = String(body.last4).replace(/\D/g, "").slice(-4);
      if (body.due_day !== undefined) c.due_day = intField(body, "due_day", "The due day", 1, 28);
      if (body.close_day !== undefined) c.close_day = intField(body, "close_day", "The closing day", 1, 31);
      if (body.autopay !== undefined) {
        if (!["min", "none", "full"].includes(String(body.autopay))) throw invalid("Autopay must be minimum, full balance or off.");
        c.autopay = String(body.autopay);
      }
      if (body.statement_min_cents !== undefined) c.statement_min_cents = intField(body, "statement_min_cents", "The minimum payment");
      refreshInstruction(acc);
      return cardView(c, acc.today);
    }
    if (method === "DELETE" && !seg[2]) {
      const before = totalBurn(liveCards(acc));
      c.archived = true;
      pushHistory(acc, `Removed ${c.name}`, before);
      acc.asks = acc.asks.filter((a) => a.card_id !== c.id || a.status !== "queued");
      acc.instruction = acc.instruction?.status === "paid" ? acc.instruction : null;
      refreshInstruction(acc);
      return { ok: true };
    }
    if (method === "POST" && seg[2] === "charges") {
      const amount = intField(body, "amount_cents", "The charge", 1);
      const before = totalBurn(liveCards(acc));
      c.balance_cents += amount;
      c.cycle_purchases_cents += amount;
      const ev = recordEvent(acc, c, "purchase", amount, "manual", body.note ? String(body.note).slice(0, 80) : "New charge");
      if (typeof body.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(body.date) && body.date <= acc.today) ev.date = body.date;
      pushHistory(acc, `New charge, ${usdShort(amount)} on ${shortName(c)}`, before);
      refreshInstruction(acc);
      return cardView(c, acc.today);
    }
    if (method === "GET" && seg[2] === "ledger") {
      const events = acc.ledger.filter((e) => e.card_id === c.id).map(({ card_id: _c, ...e }) => {
        void _c;
        return e;
      });
      return { events: events.reverse().sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0)) };
    }
    if (method === "GET" && seg[2] === "refinance") {
      const raw = r.query.get("monthly_payment_cents");
      const budget = Math.round((acc.schedule.per_payday_cents * PAYDAYS_PER_YEAR[acc.schedule.frequency]) / 12);
      const payment = raw ? Number(raw) : Math.max(minPayment(c), Math.min(budget, c.balance_cents));
      if (!Number.isFinite(payment) || payment < 100) throw invalid("Enter a monthly payment of at least $1.");
      return refinance(c, payment);
    }
  }

  // statements
  if (is("POST", "/statements")) {
    const form = r.form;
    if (!form) throw invalid("Send the statement as a file or as text.");
    const c = card(acc, String(form.get("card_id") ?? ""));
    const file = form.get("file");
    const text = form.get("text");
    if (file instanceof File) {
      if (file.size > 10 * 1024 * 1024) throw invalid("That file is larger than 10 MB. Try a photo of the summary page.");
      if (!/pdf|png|jpe?g/i.test(file.type || file.name)) throw invalid("Upload a PDF, PNG or JPG.");
      return statementView(newUpload(acc, c, "file", null));
    }
    if (typeof text === "string" && text.trim()) return statementView(newUpload(acc, c, "text", text));
    throw invalid("Add a file or paste the statement text.");
  }
  if (is("POST", "/statements/manual")) {
    const c = card(acc, String(body.card_id ?? ""));
    const fields = (body.fields ?? {}) as MStatement["fields"];
    if (typeof fields.new_balance_cents !== "number" && typeof fields.interest_cents !== "number") throw invalid("Enter at least the new balance or the interest charged.");
    const st = newUpload(acc, c, "text", "manual entry of statement numbers");
    st.source = "manual";
    delete st.ready_at;
    st.fields = fields;
    st.close_date = fields.close_date ?? st.close_date;
    st.due_date = fields.due_date ?? st.due_date;
    st.checks = runChecks(fields);
    const failing = st.checks.some((k) => k.ok === false);
    if (failing) {
      st.status = "needs_confirmation";
      st.confirm_fields = ["interest_cents", "new_balance_cents"].filter((k) => typeof fields[k as keyof typeof fields] === "number");
    } else reconcile(acc, st);
    return statementView(st);
  }
  if (is("GET", "/statements")) {
    for (const st of acc.statements) settle(acc, st);
    const cardId = r.query.get("card_id");
    return { statements: acc.statements.filter((s) => !cardId || s.card_id === cardId).map(statementView) };
  }
  if (seg[0] === "statements" && seg[1]) {
    const st = acc.statements.find((s) => s.id === seg[1]);
    if (!st) throw notFound("We couldn't find that statement.");
    if (method === "GET" && !seg[2]) {
      settle(acc, st);
      return statementView(st);
    }
    if (method === "POST" && seg[2] === "confirm") {
      if (st.status === "extracting") throw new HttpError(409, "conflict", "This statement is still being read.");
      st.fields = { ...st.fields, ...((body.fields ?? {}) as MStatement["fields"]) };
      st.close_date = st.fields.close_date ?? st.close_date;
      st.due_date = st.fields.due_date ?? st.due_date;
      const checks = runChecks(st.fields);
      const bad = checks.find((k) => k.ok === false);
      if (bad && bad.name !== "The dates are in order") {
        st.checks = checks;
        st.status = "needs_confirmation";
        st.confirm_fields = bad.name === "The balance adds up" ? ["new_balance_cents", "previous_balance_cents", "interest_cents"] : ["interest_cents", "apr_bps", "days_in_cycle"];
        return statementView(st);
      }
      reconcile(acc, st);
      return statementView(st);
    }
  }

  // asks
  if (is("GET", "/asks")) return { asks: acc.asks.map(askView), sprint: sprint(acc) };
  if (is("GET", "/playbook")) {
    const issuer = r.query.get("issuer");
    const type = r.query.get("type");
    const match = acc.asks.find((a) => (!issuer || a.issuer === issuer) && (!type || a.type === type));
    return match ? match.playbook : { entries: [], count: 0 };
  }
  if (seg[0] === "asks" && seg[1]) {
    const a = ask(acc, seg[1]);
    if (method === "GET" && !seg[2]) return askDetail(a);
    if (method === "POST" && seg[2] === "skip") {
      a.status = "skipped";
      return askView(a);
    }
    if (method === "POST" && seg[2] === "outcome") {
      const o = body as unknown as Outcome;
      if (!["granted", "partial", "temporary", "refused", "no_answer"].includes(o.result)) throw invalid("Choose what happened.");
      if (!["phone", "chat", "message"].includes(o.channel)) throw invalid("Choose how you asked.");
      if (a.type === "apr_reduction" && (o.result === "granted" || o.result === "temporary" || o.result === "partial")) {
        if (typeof o.new_apr_bps !== "number") throw invalid("Enter the new rate the rep gave you.");
        const c = acc.cards.find((x) => x.id === a.card_id);
        if (c && o.new_apr_bps >= c.apr_bps) throw invalid(`The new rate should be lower than ${pct(c.apr_bps)}.`);
      }
      if (o.result === "temporary" && !o.until) throw invalid("Enter the date the temporary rate ends.");
      applyOutcome(acc, a, o);
      return { ask: askView(a), today: todayView(acc) };
    }
  }

  // rehearsal
  if (is("POST", "/rehearsal")) {
    const a = ask(acc, String(body.ask_id ?? ""));
    const id = nextId("rh", acc);
    acc.rehearsals[id] = {
      id, ask_id: a.id, scenario: a.rep.scenario, provider: "fake", finished: false, turns: 0,
      messages: [{ role: "rep", text: a.rep.opening }],
      checklist: a.must.map((m) => ({ id: m.id, label: m.label, done: false })),
    };
    return rehearsalView(acc.rehearsals[id]);
  }
  if (seg[0] === "rehearsal" && seg[1]) {
    const rh = acc.rehearsals[seg[1]];
    if (!rh) throw notFound("This practice session has ended. Start a new one.");
    if (method === "GET" && !seg[2]) return rehearsalView(rh);
    if (method === "POST" && seg[2] === "turn") {
      const text = String(body.text ?? "").trim();
      if (!text) throw invalid("Type what you would say.");
      if (text.length > 1000) throw invalid("That is longer than a rep would listen to. Keep it under 1,000 characters.");
      if (rh.finished) throw new HttpError(409, "conflict", "This practice call has ended. Start a new one.");
      const a = ask(acc, rh.ask_id);
      rh.messages.push({ role: "you", text });
      const reply = repReply(a, rh, text);
      rh.messages.push({ role: "rep", text: reply });
      const pieces = reply.split(/(?<=\s)/);
      return sse(pieces, () => rehearsalView(rh));
    }
  }

  throw notFound(`No mock for ${method} /api${path}.`);
}

/** Handles one /api call. Always answers with a real Response, like a server would. */
export async function handle(url: URL, init: RequestInit | undefined): Promise<Response> {
  const d = load();
  const method = (init?.method ?? "GET").toUpperCase();
  const path = url.pathname.replace(/^\/api/, "") || "/";
  let body: Record<string, unknown> = {};
  let form: FormData | null = null;
  if (init?.body instanceof FormData) form = init.body;
  else if (typeof init?.body === "string" && init.body) {
    try {
      body = JSON.parse(init.body) as Record<string, unknown>;
    } catch {
      body = {};
    }
  }
  // A little latency keeps loading states honest without slowing the click-through.
  await new Promise((r) => setTimeout(r, method === "GET" ? 60 : 140));
  try {
    const out = route(d, { method, path, query: url.searchParams, body, form });
    save();
    if (out instanceof Response) return out;
    return new Response(JSON.stringify(out), { status: 200, headers: { "Content-Type": "application/json" } });
  } catch (e) {
    save();
    if (e instanceof HttpError) {
      return new Response(JSON.stringify({ error: { code: e.code, message: e.message } }), { status: e.status, headers: { "Content-Type": "application/json" } });
    }
    console.error("[mock api]", e);
    return new Response(JSON.stringify({ error: { code: "mock_error", message: "The mock server hit an unexpected error." } }), { status: 500, headers: { "Content-Type": "application/json" } });
  }
}
