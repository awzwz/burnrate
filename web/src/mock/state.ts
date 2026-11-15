// Mock state: one browser, one "server". Kept in localStorage so a reload resumes where it was.
import type {
  BurnPoint,
  CardInput,
  Checkout,
  InboxMessage,
  Instruction,
  LedgerEvent,
  MeterItem,
  NotificationPrefs,
  Plan,
  QuizAnswers,
  Rehearsal,
  Result,
  ScheduleInput,
  Statement,
  Subscription,
} from "../api/types";
import { dateLong, dateShort, pct, usd, usdShort } from "../lib/format";
import type { MockAsk } from "./content";
import { buildAsk } from "./content";
import {
  addDays,
  buildInstruction,
  burnOf,
  diffDays,
  guessIssuer,
  issuerName,
  minPayment,
  nextDayOfMonth,
  paydays,
  realToday,
  round4,
  shortName,
  totalBurn,
} from "./engine";

export interface MCard {
  id: string;
  name: string;
  issuer: string;
  last4: string;
  balance_cents: number;
  apr_bps: number;
  due_day: number;
  close_day: number;
  autopay: string;
  statement_min_cents: number | null;
  start_balance_cents: number;
  /** Payments since the last statement closed: decides whether autopay still has a minimum to pay. */
  cycle_paid_cents: number;
  cycle_purchases_cents: number;
  cycle_start_balance_cents: number;
  last_close: string;
  /** Snapshot of the most recent closed cycle, the source for a statement's numbers. */
  last_statement: {
    close_date: string;
    days: number;
    previous_balance_cents: number;
    payments_cents: number;
    purchases_cents: number;
    fees_cents: number;
    interest_cents: number;
    new_balance_cents: number;
    apr_bps: number;
    min_payment_cents: number;
  } | null;
  archived?: boolean;
}

export interface MLedger extends LedgerEvent {
  card_id: string;
}

export interface MStatement extends Statement {
  ready_at?: number;
  resolve_to?: "needs_confirmation" | "reconciled" | "failed";
}

export interface MRehearsal extends Rehearsal {
  turns: number;
}

export interface RateWin {
  card_id: string;
  from_bps: number;
  to_bps: number;
  date: string;
  until: string | null;
  label: string;
}

export interface Account {
  demo: boolean;
  email: string | null;
  today: string;
  real_today: string;
  started_on: string;
  story_start: string;
  cards: MCard[];
  ledger: MLedger[];
  schedule: ScheduleInput;
  history: BurnPoint[];
  burn_start: number;
  instruction: Instruction | null;
  /** "Short this payday": the amount the user said is available, for one payday. */
  short: { payday: string; available_cents: number } | null;
  asks: MockAsk[];
  sprint_ends: string;
  statements: MStatement[];
  rehearsals: Record<string, MRehearsal>;
  inbox: InboxMessage[];
  meter_items: MeterItem[];
  rate_wins: RateWin[];
  subscription: Subscription;
  prefs: NotificationPrefs;
  seq: number;
}

export interface DB {
  v: number;
  user_id: string;
  mode: "anon" | "demo" | "paid";
  email: string | null;
  demo: Account | null;
  paid: Account | null;
  quiz: { answers: QuizAnswers; cards: CardInput[]; schedule: ScheduleInput | null; result: Result | null };
  checkouts: Record<string, Checkout & { email: string }>;
  login_tokens: Record<string, string>;
  dev_inbox: InboxMessage[];
  seq: number;
}

const KEY = "br-mock-v3";
const VERSION = 3;

export const PLANS: Plan[] = [
  { id: "w1", name: "1-week plan", price_cents: 693, days: 7, renews_price_cents: 693, renews_days: 7, per_day_cents: 99, then: "then $6.93 every week", tag: null },
  { id: "w4", name: "4-week plan", price_cents: 1999, days: 28, renews_price_cents: 3999, renews_days: 28, per_day_cents: 71.3929, then: "then $39.99 every 4 weeks", tag: null },
  { id: "w12", name: "12-week plan", price_cents: 3999, days: 84, renews_price_cents: 7999, renews_days: 84, per_day_cents: 47.6071, then: "then $79.99 every 12 weeks", tag: "Full program" },
];
export const GUARANTEE_DAYS = 14;
export const TEST_CARD = "4242 4242 4242 4242";

function fresh(): DB {
  return {
    v: VERSION,
    user_id: `u_${Math.random().toString(36).slice(2, 10)}`,
    mode: "anon",
    email: null,
    demo: null,
    paid: null,
    quiz: { answers: {}, cards: [], schedule: null, result: null },
    checkouts: {},
    login_tokens: {},
    dev_inbox: [],
    seq: 1,
  };
}

let db: DB | null = null;

export function load(): DB {
  if (db) return db;
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as DB;
      if (parsed.v === VERSION) {
        db = parsed;
        return db;
      }
    }
  } catch {
    /* unreadable storage: start clean */
  }
  db = fresh();
  return db;
}

export function save(): void {
  if (!db) return;
  try {
    localStorage.setItem(KEY, JSON.stringify(db));
  } catch {
    /* storage full or blocked: the mock keeps working in memory */
  }
}

export function nextId(prefix: string, holder: { seq: number }): string {
  holder.seq += 1;
  return `${prefix}_${holder.seq.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

export function account(d: DB): Account | null {
  if (d.mode === "demo") return d.demo;
  if (d.mode === "paid") return d.paid;
  return null;
}

// ---------- building an account ----------
function ledgerEvent(acc: Account, card_id: string, type: string, amount: number, date: string, source: string, note: string | null): MLedger {
  const credit = type === "payment" || type === "credit";
  const ev: MLedger = { id: nextId("ev", acc), card_id, type, amount_cents: amount, signed_cents: credit ? -amount : amount, date, source, note };
  acc.ledger.push(ev);
  return ev;
}

export function recordEvent(acc: Account, card: MCard, type: string, amount: number, source: string, note: string | null): MLedger {
  return ledgerEvent(acc, card.id, type, amount, acc.today, source, note);
}

export function liveCards(acc: Account): MCard[] {
  return acc.cards.filter((c) => !c.archived);
}

export function pushHistory(acc: Account, label: string, before: number): void {
  const now = totalBurn(liveCards(acc));
  acc.history.push({ date: acc.today, value: round4(now), label, delta: round4(now - before) });
}

export function currentPayday(acc: Account): { payday: string; isToday: boolean } {
  const next = paydays(acc.schedule, acc.today, 1)[0];
  return { payday: next, isToday: next === acc.today };
}

/** Rebuilds the instruction for the payday that is due today or comes next. A paid one is kept for its day. */
export function refreshInstruction(acc: Account): void {
  const cur = acc.instruction;
  if (cur && cur.status === "paid" && cur.payday === acc.today) return;
  const cards = liveCards(acc);
  const last4 = Object.fromEntries(cards.map((c) => [c.id, c.last4]));
  // An unpaid instruction from a payday that has passed stays open until the next payday arrives.
  const openPast = cur && cur.status !== "paid" && cur.payday < acc.today && cur.next_payday > acc.today;
  const payday = openPast ? cur.payday : paydays(acc.schedule, acc.today, 1)[0];
  const short = acc.short && acc.short.payday === payday ? acc.short.available_cents : undefined;
  const built = buildInstruction(cards, acc.schedule, payday, { last4, available_cents: short });
  if (!built) {
    acc.instruction = null;
    return;
  }
  const keepId = cur && cur.payday === payday && cur.status !== "paid" ? cur.id : nextId("ins", acc);
  const status: Instruction["status"] = payday > acc.today ? "upcoming" : short !== undefined ? "short" : "due";
  acc.instruction = { ...built, id: keepId, status, paid_at: null, paid_cents: null };
}

function sms(acc: Account, body: string, link: string | null, daysAgo = 0, hour = 8): void {
  acc.inbox.unshift({ id: nextId("msg", acc), channel: "sms", to: acc.prefs.phone ?? "+1 (555) 010-0142", subject: null, body, link, sent_at: stamp(addDays(acc.today, -daysAgo), hour) });
}
function email(acc: Account, subject: string, body: string, link: string | null, daysAgo = 0, hour = 9): void {
  acc.inbox.unshift({ id: nextId("msg", acc), channel: "email", to: acc.email ?? "maya@example.com", subject, body, link, sent_at: stamp(addDays(acc.today, -daysAgo), hour) });
}
/** A timestamp at `hour` o'clock on that date in the viewer's own time zone. */
export function stamp(iso: string, hour = 9): string {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d, hour, 0, 0).toISOString();
}

export function paydayMessage(acc: Account): void {
  const ins = acc.instruction;
  if (!ins || ins.status === "upcoming" || !ins.sends[0]) return;
  const s = ins.sends[0];
  sms(acc, `Burn Rate: it's payday. Send ${usd(s.amount_cents)} to ${s.card_name} today. Autopay covers the other minimums. Reply STOP to opt out.`, "/app");
}
export function statementMessage(acc: Account, card: MCard, statementId: string | null): void {
  email(
    acc,
    `Your ${card.name} statement closed on ${dateShort(acc.today)}`,
    `Add the statement and we will put our forecast next to ${issuerName(card)}'s own "Interest Charged" line. It takes about a minute.`,
    statementId ? `/app/statements/${statementId}` : "/app/statements",
  );
}

function subscriptionFor(plan: Plan, startedISO: string): Subscription {
  return {
    status: "active",
    plan,
    started_at: stamp(startedISO, 10),
    current_period_end: stamp(addDays(startedISO, plan.days), 10),
    cancel_at_period_end: false,
    paused_until: null,
    renews_price_cents: plan.renews_price_cents,
    paid_total_cents: plan.price_cents,
    refundable_until: addDays(startedISO, GUARANTEE_DAYS),
  };
}

function emptyAccount(demo: boolean, today: string, schedule: ScheduleInput, plan: Plan, startedOn: string, mail: string | null): Account {
  return {
    demo,
    email: mail,
    today,
    real_today: realToday(),
    started_on: startedOn,
    story_start: today,
    cards: [],
    ledger: [],
    schedule,
    history: [],
    burn_start: 0,
    instruction: null,
    short: null,
    asks: [],
    sprint_ends: addDays(startedOn, 14),
    statements: [],
    rehearsals: {},
    inbox: [],
    meter_items: [],
    rate_wins: [],
    subscription: subscriptionFor(plan, startedOn),
    prefs: { phone: null, sms_opt_in: false, email_opt_in: true, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "America/New_York", calendar_url: null },
    seq: 1,
  };
}

function lastCloseBefore(closeDay: number, today: string): string {
  // The most recent date on or before today whose day of month is closeDay.
  const upcoming = nextDayOfMonth(closeDay, today);
  if (upcoming === today) return today;
  return nextDayOfMonth(closeDay, addDays(upcoming, -35));
}

export function makeCard(acc: Account, input: CardInput & { last4?: string; autopay?: string }, id: string): MCard {
  const issuer = input.issuer && input.issuer !== "other" ? input.issuer : guessIssuer(input.name);
  const close_day = input.close_day ?? ((input.due_day + 2) % 28) + 1;
  const last_close = lastCloseBefore(close_day, acc.today);
  return {
    id,
    name: input.name.trim(),
    issuer,
    last4: input.last4 ?? "",
    balance_cents: input.balance_cents,
    apr_bps: input.apr_bps,
    due_day: input.due_day,
    close_day,
    autopay: input.autopay ?? "min",
    statement_min_cents: input.statement_min_cents ?? null,
    start_balance_cents: input.balance_cents,
    cycle_paid_cents: 0,
    cycle_purchases_cents: 0,
    cycle_start_balance_cents: input.balance_cents,
    last_close,
    last_statement: null,
  };
}

/** The sample persona, dated from the first Friday on or after the real today, like the server does. */
export function seedDemo(): Account {
  const real = realToday();
  const dow = new Date(`${real}T12:00:00`).getDay();
  const d0 = addDays(real, (5 - dow + 7) % 7);
  const day = (n: number) => addDays(d0, n);
  const dom = (n: number, cap = 28) => Math.min(Number(day(n).slice(8, 10)), cap);
  const acc = emptyAccount(true, d0, { frequency: "biweekly", anchor: d0, per_payday_cents: 30000, second_day: 15 }, PLANS[2], day(-42), "maya@example.com");
  acc.prefs.phone = "+1 (555) 010-0142";
  acc.prefs.sms_opt_in = true;

  const quick = makeCard(acc, { name: "Capital One Quicksilver", issuer: "capital_one", last4: "4417", balance_cents: 520000, apr_bps: 2799, due_day: dom(19), close_day: dom(-9, 31), statement_min_cents: 17133 }, "c1");
  const chase = makeCard(acc, { name: "Chase Freedom Unlimited", issuer: "chase", last4: "0932", balance_cents: 340000, apr_bps: 2249, due_day: dom(16), close_day: dom(-11, 31) }, "c2");
  const disc = makeCard(acc, { name: "Discover it", issuer: "discover", last4: "7781", balance_cents: 190000, apr_bps: 2499, due_day: dom(7), close_day: dom(-21, 31) }, "c3");
  quick.start_balance_cents = 568000;
  quick.last_close = day(-9);
  chase.last_close = day(-11);
  disc.last_close = day(-21);
  quick.last_statement = { close_date: day(-9), days: 30, previous_balance_cents: 532067, payments_cents: 24000, purchases_cents: 0, fees_cents: 0, interest_cents: 11933, new_balance_cents: 520000, apr_bps: 2799, min_payment_cents: 17133 };
  chase.last_statement = { close_date: day(-11), days: 30, previous_balance_cents: 329615, payments_cents: 0, purchases_cents: 0, fees_cents: 3900, interest_cents: 6485, new_balance_cents: 340000, apr_bps: 2249, min_payment_cents: 9685 };
  disc.last_statement = { close_date: day(-21), days: 30, previous_balance_cents: 192097, payments_cents: 6000, purchases_cents: 0, fees_cents: 0, interest_cents: 3903, new_balance_cents: 190000, apr_bps: 2499, min_payment_cents: 6000 };
  acc.cards = [quick, chase, disc];

  // History: she subscribed 42 days before d0, made two payday payments and one rate request that worked.
  ledgerEvent(acc, "c1", "opening", 568000, day(-42), "quiz", "Balance when you started");
  ledgerEvent(acc, "c2", "opening", 340000, day(-42), "quiz", "Balance when you started");
  ledgerEvent(acc, "c3", "opening", 190000, day(-42), "quiz", "Balance when you started");
  ledgerEvent(acc, "c1", "payment", 24000, day(-28), "payday", "Payday payment");
  ledgerEvent(acc, "c2", "rate_change", 0, day(-17), "ask", "Chase lowered your rate 24.49% → 22.49%");
  ledgerEvent(acc, "c1", "payment", 24000, day(-14), "payday", "Payday payment");
  const b0 = burnOf({ balance_cents: 568000, apr_bps: 2799 }) + burnOf({ balance_cents: 340000, apr_bps: 2449 }) + burnOf(disc);
  const b1 = b0 - burnOf({ balance_cents: 24000, apr_bps: 2799 });
  const b2 = b1 - burnOf({ balance_cents: 340000, apr_bps: 200 });
  const b3 = totalBurn(acc.cards);
  acc.burn_start = round4(b0);
  acc.history = [
    { date: day(-42), value: round4(b0), label: "Start", delta: 0 },
    { date: day(-28), value: round4(b1), label: "Payday, $240 to Quicksilver", delta: round4(b1 - b0) },
    { date: day(-17), value: round4(b2), label: "Chase lowered your rate to 22.49%", delta: round4(b2 - b1) },
    { date: day(-14), value: round4(b3), label: "Payday, $240 to Quicksilver", delta: round4(b3 - b2) },
  ];
  acc.rate_wins = [{ card_id: "c2", from_bps: 2449, to_bps: 2249, date: day(-17), until: null, label: "Chase lowered your rate 24.49% → 22.49%" }];
  acc.meter_items = [
    { label: "Paid on payday instead of the due date", cents: 350, kind: "estimated", date: day(-28) },
    { label: "Paid on payday instead of the due date", cents: 349, kind: "estimated", date: day(-14) },
  ];

  const done = buildAsk("apr_reduction", { ...chase, apr_bps: 2449 }, 1);
  done.status = "done";
  done.result = "granted";
  done.subtitle = "Chase lowered your rate 24.49% → 22.49%";
  done.value_label = "22.49%";
  const late = buildAsk("late_fee_refund", chase, 2, { late_fee_cents: 3900 });
  late.subtitle = "The fee on your October statement";
  const apr = buildAsk("apr_reduction", quick, 3);
  const due = buildAsk("due_date_move", disc, 4, { next_due: nextDayOfMonth(disc.due_day, d0), payday_note: "two days after payday" });
  acc.asks = [done, late, apr, due];
  acc.sprint_ends = day(14);

  email(acc, "Your Burn Rate receipt and renewal terms", `12-week plan, $39.99. It renews on ${dateShort(day(42))} at $79.99 every 12 weeks until you cancel. Cancel online in Settings in two steps. 14-day money-back, no conditions.`, "/app/settings", 42);
  sms(acc, "Burn Rate: it's payday. Send $240.00 to Capital One Quicksilver today. Autopay covers the other minimums. Reply STOP to opt out.", "/app", 28);
  email(acc, "Logged: Chase lowered your rate to 22.49%", "Your daily burn dropped by $0.19. The next Chase statement should show the new rate; we will check it for you.", "/app/asks", 17);
  sms(acc, "Burn Rate: it's payday. Send $240.00 to Capital One Quicksilver today. Autopay covers the other minimums. Reply STOP to opt out.", "/app", 14);

  refreshInstruction(acc);
  paydayMessage(acc);
  return acc;
}

/** After the test checkout: the account is built from the quiz the visitor filled in. */
export function seedFromQuiz(d: DB, plan: Plan, mail: string): Account {
  const today = realToday();
  const schedule = d.quiz.schedule ?? { frequency: "biweekly", anchor: today, per_payday_cents: 20000 };
  const acc = emptyAccount(false, today, schedule, plan, today, mail);
  acc.cards = d.quiz.cards.map((c, i) => makeCard(acc, c, `c${i + 1}`));
  for (const c of acc.cards) ledgerEvent(acc, c.id, "opening", c.balance_cents, today, "quiz", "Balance when you started");
  const burn = totalBurn(acc.cards);
  acc.burn_start = round4(burn);
  acc.history = [{ date: today, value: round4(burn), label: "Start", delta: 0 }];

  const answers = d.quiz.answers;
  const asks: MockAsk[] = [];
  const byBalance = [...acc.cards].sort((a, b) => b.balance_cents - a.balance_cents);
  if (answers.late_fee_recently) {
    const card = acc.cards.find((c) => c.name === answers.late_fee_card) ?? byBalance[0];
    if (card) asks.push(buildAsk("late_fee_refund", card, asks.length + 1, { late_fee_cents: Number(answers.late_fee_cents) || 3900 }));
  }
  for (const card of byBalance.slice(0, 2)) asks.push(buildAsk("apr_reduction", card, asks.length + 1));
  const payday = paydays(schedule, today, 1)[0];
  const far = acc.cards.find((c) => {
    const gap = diffDays(payday, nextDayOfMonth(c.due_day, payday));
    return gap >= 5 && gap <= 13;
  });
  if (far) asks.push(buildAsk("due_date_move", far, asks.length + 1, { next_due: nextDayOfMonth(far.due_day, today), payday_note: "two days after payday" }));
  acc.asks = asks;

  refreshInstruction(acc);
  email(acc, "Your Burn Rate receipt and renewal terms", `${plan.name}, ${usd(plan.price_cents)}. It renews on ${dateShort(addDays(today, plan.days))} at ${usd(plan.renews_price_cents)}, ${plan.then.replace("then ", "")}, until you cancel. Cancel online in Settings in two steps. ${GUARANTEE_DAYS}-day money-back, no conditions.`, "/app/settings");
  return acc;
}

// ---------- the demo clock ----------
/** Moves the account's date forward one day at a time and lets due dates, closes and paydays happen. */
export function advanceTo(acc: Account, target: string): string[] {
  const notes: string[] = [];
  while (acc.today < target) {
    acc.today = addDays(acc.today, 1);
    const day = acc.today;
    for (const card of liveCards(acc)) {
      if (card.balance_cents <= 0) continue;
      if (nextDayOfMonth(card.due_day, day) === day && card.autopay !== "none") {
        const min = card.last_statement ? Math.min(card.last_statement.min_payment_cents, card.balance_cents) : minPayment(card);
        const owed = card.autopay === "full" ? card.balance_cents : Math.max(0, min - card.cycle_paid_cents);
        if (owed > 0) {
          const before = totalBurn(liveCards(acc));
          card.balance_cents -= owed;
          card.cycle_paid_cents += owed;
          recordEvent(acc, card, "payment", owed, "autopay", "Autopay, minimum payment");
          pushHistory(acc, `Autopay, ${usdShort(owed)} to ${shortName(card)}`, before);
          notes.push(`Autopay paid ${usd(owed)} to ${card.name} on ${dateShort(day)}.`);
        }
      }
      if (nextDayOfMonth(card.close_day, day) === day && card.last_close !== day) {
        const days = Math.max(1, diffDays(card.last_close, day));
        const interest = Math.round(burnOf(card) * days);
        const before = totalBurn(liveCards(acc));
        const prev = card.last_statement?.new_balance_cents ?? card.cycle_start_balance_cents;
        card.balance_cents += interest;
        recordEvent(acc, card, "interest", interest, "forecast", `Interest for ${days} days, our forecast`);
        pushHistory(acc, `Interest added, ${shortName(card)}`, before);
        card.last_statement = {
          close_date: day,
          days,
          previous_balance_cents: prev,
          payments_cents: card.cycle_paid_cents,
          purchases_cents: card.cycle_purchases_cents,
          fees_cents: 0,
          interest_cents: interest,
          new_balance_cents: card.balance_cents,
          apr_bps: card.apr_bps,
          min_payment_cents: minPayment({ ...card, statement_min_cents: null }),
        };
        if (card.statement_min_cents !== null) card.statement_min_cents = card.last_statement.min_payment_cents;
        card.cycle_paid_cents = 0;
        card.cycle_purchases_cents = 0;
        card.cycle_start_balance_cents = card.balance_cents;
        card.last_close = day;
        const st = acc.demo ? demoStatement(acc, card) : null;
        statementMessage(acc, card, st?.id ?? null);
        notes.push(`Your ${card.name} statement closed on ${dateShort(day)}${st ? "; it is waiting in Statements" : ""}.`);
      }
    }
    if (paydays(acc.schedule, day, 1)[0] === day) {
      acc.instruction = null;
      refreshInstruction(acc);
      paydayMessage(acc);
      notes.push(`${dateLong(day)} is a payday.`);
    }
  }
  refreshInstruction(acc);
  return notes;
}

// ---------- statements ----------
export function statementFields(card: MCard, acc: Account): MStatement["fields"] {
  const s = card.last_statement;
  if (!s) {
    return { new_balance_cents: card.balance_cents, apr_bps: card.apr_bps, min_payment_cents: minPayment(card), close_date: card.last_close, due_date: nextDayOfMonth(card.due_day, card.last_close) };
  }
  const bsi = s.apr_bps > 0 ? Math.round((s.interest_cents * 365 * 10000) / (s.apr_bps * s.days)) : s.new_balance_cents;
  const ytd = acc.ledger.filter((e) => e.card_id === card.id && e.type === "interest").reduce((sum, e) => sum + e.amount_cents, 0);
  return {
    previous_balance_cents: s.previous_balance_cents,
    payments_credits_cents: s.payments_cents,
    purchases_cents: s.purchases_cents,
    fees_cents: s.fees_cents,
    interest_cents: s.interest_cents,
    new_balance_cents: s.new_balance_cents,
    apr_bps: s.apr_bps,
    balance_subject_to_interest_cents: bsi,
    days_in_cycle: s.days,
    min_payment_cents: s.min_payment_cents,
    close_date: s.close_date,
    due_date: nextDayOfMonth(card.due_day, addDays(s.close_date, 1)),
    late_fee_cents: s.fees_cents,
    interest_ytd_cents: Math.max(ytd, s.interest_cents * 10),
  };
}

export function runChecks(f: MStatement["fields"]): MStatement["checks"] {
  const has = (...keys: (keyof MStatement["fields"])[]) => keys.every((k) => f[k] !== null && f[k] !== undefined);
  const checks: MStatement["checks"] = [];
  if (has("previous_balance_cents", "new_balance_cents", "interest_cents")) {
    const expected = (f.previous_balance_cents ?? 0) - (f.payments_credits_cents ?? 0) + (f.purchases_cents ?? 0) + (f.fees_cents ?? 0) + (f.interest_cents ?? 0);
    const ok = Math.abs(expected - (f.new_balance_cents ?? 0)) <= 1;
    checks.push({ name: "The balance adds up", ok, detail: ok ? "Previous balance, minus payments, plus purchases, fees and interest equals the new balance." : `The lines add up to ${usd(expected)}, but the new balance reads ${usd(f.new_balance_cents)}.` });
  } else checks.push({ name: "The balance adds up", ok: null, detail: "Needs the previous balance, the new balance and the interest charged." });
  if (has("balance_subject_to_interest_cents", "apr_bps", "days_in_cycle", "interest_cents")) {
    const calc = Math.round(((f.balance_subject_to_interest_cents ?? 0) * (f.apr_bps ?? 0) * (f.days_in_cycle ?? 0)) / 10000 / 365);
    const ok = Math.abs(calc - (f.interest_cents ?? 0)) <= 50;
    checks.push({ name: "The interest method matches", ok, detail: ok ? `Balance subject to interest × APR ÷ 365 × days gives ${usd(calc)}; the statement says ${usd(f.interest_cents)}.` : `Our arithmetic gives ${usd(calc)}, the statement says ${usd(f.interest_cents)}. Please check the two numbers.` });
  } else checks.push({ name: "The interest method matches", ok: null, detail: "Needs the balance subject to interest, the APR and the days in the cycle." });
  if (has("min_payment_cents", "new_balance_cents")) {
    const ok = (f.min_payment_cents ?? 0) <= (f.new_balance_cents ?? 0);
    checks.push({ name: "The minimum is below the balance", ok, detail: ok ? "The minimum payment is smaller than the new balance." : "The minimum payment reads larger than the balance." });
  } else checks.push({ name: "The minimum is below the balance", ok: null, detail: "Needs the minimum payment and the new balance." });
  if (has("close_date", "due_date")) {
    const gap = diffDays(f.close_date as string, f.due_date as string);
    const ok = gap >= 21 && gap <= 31;
    checks.push({ name: "The dates are in order", ok, detail: ok ? `The due date is ${gap} days after the closing date.` : `The due date is ${gap} days after the closing date; issuers give at least 21.` });
  } else checks.push({ name: "The dates are in order", ok: null, detail: "Needs the closing date and the due date." });
  if (has("apr_bps")) {
    const ok = (f.apr_bps ?? 0) > 0 && (f.apr_bps ?? 0) < 4000;
    checks.push({ name: "The rate is in a plausible range", ok, detail: ok ? `${pct(f.apr_bps)} is within the range issuers charge.` : `${pct(f.apr_bps)} is outside the range issuers charge. Please check it.` });
  } else checks.push({ name: "The rate is in a plausible range", ok: null, detail: "Needs the APR." });
  return checks;
}

export function reconcile(acc: Account, st: MStatement): void {
  const card = acc.cards.find((c) => c.id === st.card_id);
  if (!card) return;
  const predicted = card.last_statement?.interest_cents ?? Math.round(burnOf(card) * 30);
  const actual = st.fields.interest_cents ?? predicted;
  const diff = actual - predicted;
  const parts: { label: string; cents: number }[] = [];
  let left = diff;
  const knownPurchases = card.last_statement?.purchases_cents ?? 0;
  const newPurchases = (st.fields.purchases_cents ?? knownPurchases) - knownPurchases;
  if (newPurchases > 0 && left > 0) {
    const share = Math.min(left, Math.round((newPurchases * card.apr_bps * 15) / 10000 / 365));
    if (share > 0) {
      parts.push({ label: `Purchases we did not know about (${usd(newPurchases)})`, cents: share });
      left -= share;
    }
  }
  if (Math.abs(left) > 2) {
    const posting = left > 0 ? Math.min(left, 12) : Math.max(left, -12);
    parts.push({ label: left > 0 ? "A payment posted later than the day you marked it" : "A payment posted earlier than the day you marked it", cents: posting });
    left -= posting;
  }
  if (Math.abs(left) > 2) {
    parts.push({ label: "Not explained yet; we will ask you about it", cents: left - Math.sign(left) });
    left = Math.sign(left);
  }
  parts.push({ label: "Rounding", cents: left });
  const within = Math.abs(diff) <= 50;
  st.reconciliation = {
    predicted_interest_cents: predicted,
    actual_interest_cents: actual,
    diff_cents: diff,
    parts,
    summary: diff === 0
      ? "Our forecast matched the statement to the cent."
      : within
        ? `Our forecast was ${usd(Math.abs(diff))} ${diff > 0 ? "below" : "above"} the statement. The statement wins: your plan now uses the bank's numbers.`
        : `The statement differs from our forecast by ${usd(Math.abs(diff))}. The statement wins: your plan now uses the bank's numbers, and the difference is explained below.`,
    within_tolerance: within,
  };
  st.status = "reconciled";
  st.confirm_fields = [];
  st.checks = runChecks(st.fields);
  // The statement always wins: bring the ledger to the bank's balance as of the close.
  const sinceClose = acc.ledger.filter((e) => e.card_id === card.id && e.date > (st.fields.close_date ?? card.last_close)).reduce((s, e) => s + e.signed_cents, 0);
  if (typeof st.fields.new_balance_cents === "number") {
    const shouldBe = st.fields.new_balance_cents + sinceClose;
    const adj = shouldBe - card.balance_cents;
    if (adj !== 0) {
      const before = totalBurn(liveCards(acc));
      card.balance_cents = Math.max(0, shouldBe);
      const ev = recordEvent(acc, card, "adjustment", Math.abs(adj), "statement", "Brought to the statement balance");
      ev.signed_cents = adj;
      if (Math.abs(adj) >= 100) pushHistory(acc, `Statement check, ${shortName(card)}`, before);
    }
  }
  if (typeof st.fields.apr_bps === "number" && st.fields.apr_bps !== card.apr_bps && st.fields.apr_bps > 0) {
    const before = totalBurn(liveCards(acc));
    const up = st.fields.apr_bps > card.apr_bps;
    if (up) st.triggers.push({ type: "apr_reduction", title: `Your rate went up to ${pct(st.fields.apr_bps)}`, detail: `The last statement showed ${pct(card.apr_bps)}. A rate that just rose is a fair reason to ask ${issuerName(card)} for a lower one.` });
    card.apr_bps = st.fields.apr_bps;
    pushHistory(acc, `Rate on the statement: ${pct(card.apr_bps)}, ${shortName(card)}`, before);
  }
  if (typeof st.fields.min_payment_cents === "number") card.statement_min_cents = st.fields.min_payment_cents;
  if ((st.fields.late_fee_cents ?? 0) > 0 && !st.triggers.some((t) => t.type === "late_fee_refund")) {
    st.triggers.push({ type: "late_fee_refund", title: `Ask for the ${usd(st.fields.late_fee_cents)} late fee back`, detail: "A late fee is on this statement. Issuers often refund a first late fee when asked; it may say no." });
    if (!acc.asks.some((a) => a.type === "late_fee_refund" && a.card_id === card.id && a.status === "queued")) {
      acc.asks.push(buildAsk("late_fee_refund", card, acc.asks.length + 1, { late_fee_cents: st.fields.late_fee_cents ?? 0 }));
    }
  }
  refreshInstruction(acc);
}

function baseStatement(acc: Account, card: MCard, source: MStatement["source"]): MStatement {
  return {
    id: nextId("st", acc),
    card_id: card.id,
    card_name: card.name,
    status: "extracting",
    source,
    fields: {},
    checks: [],
    confirm_fields: [],
    reconciliation: null,
    triggers: [],
    error: null,
    created_at: new Date().toISOString(),
    close_date: card.last_statement?.close_date ?? card.last_close,
    due_date: nextDayOfMonth(card.due_day, addDays(card.last_statement?.close_date ?? card.last_close, 1)),
  };
}

/** What the "bank" printed: our forecast plus a small difference, so there is something to explain. */
export function bankFields(card: MCard, acc: Account): MStatement["fields"] {
  const f = statementFields(card, acc);
  if (typeof f.interest_cents === "number" && card.last_statement) {
    const bump = 4 + (card.name.length % 5);
    f.interest_cents += bump;
    if (typeof f.new_balance_cents === "number") f.new_balance_cents += bump;
    if (typeof f.balance_subject_to_interest_cents === "number" && f.apr_bps && f.days_in_cycle) {
      f.balance_subject_to_interest_cents = Math.round((f.interest_cents * 365 * 10000) / (f.apr_bps * f.days_in_cycle));
    }
  }
  return f;
}

export function demoStatement(acc: Account, card: MCard): MStatement {
  const st = baseStatement(acc, card, "demo");
  st.fields = bankFields(card, acc);
  st.status = "needs_confirmation";
  st.confirm_fields = ["interest_cents", "new_balance_cents"];
  st.checks = runChecks(st.fields);
  if (card.issuer === "discover" || card.due_day > 20) {
    st.triggers.push({ type: "due_date_move", title: "Move this due date closer to payday", detail: `This statement is due on ${dateShort(st.due_date)}. A due date right after payday keeps the minimum inside one paycheck.` });
  }
  acc.statements.unshift(st);
  return st;
}

export function newUpload(acc: Account, card: MCard, kind: "file" | "text", text: string | null): MStatement {
  const st = baseStatement(acc, card, "upload");
  st.ready_at = Date.now() + 2600;
  st.resolve_to = kind === "file" ? "needs_confirmation" : text && text.trim().length < 20 ? "failed" : "reconciled";
  acc.statements.unshift(st);
  return st;
}

/** Finishes a pending extraction once its time has come. */
export function settle(acc: Account, st: MStatement): boolean {
  if (st.status !== "extracting" || !st.ready_at || Date.now() < st.ready_at) return false;
  const card = acc.cards.find((c) => c.id === st.card_id);
  delete st.ready_at;
  if (!card || st.resolve_to === "failed") {
    st.status = "failed";
    st.error = "We could not find statement numbers in that. You can enter the numbers yourself instead.";
    return true;
  }
  st.fields = bankFields(card, acc);
  st.close_date = st.fields.close_date ?? st.close_date;
  st.due_date = st.fields.due_date ?? st.due_date;
  st.checks = runChecks(st.fields);
  if (st.resolve_to === "needs_confirmation") {
    st.status = "needs_confirmation";
    st.confirm_fields = ["interest_cents", "new_balance_cents"];
  } else reconcile(acc, st);
  return true;
}

// ---------- meter ----------
export function meter(acc: Account) {
  const items = [...acc.meter_items];
  for (const win of acc.rate_wins) {
    const card = acc.cards.find((c) => c.id === win.card_id);
    if (!card) continue;
    const end = win.until && win.until < acc.today ? win.until : acc.today;
    const days = Math.max(0, diffDays(win.date, end));
    const cents = Math.round(((card.balance_cents * (win.from_bps - win.to_bps)) / 10000 / 365) * days);
    items.push({ label: win.label, cents, kind: "confirmed", date: win.date });
  }
  const confirmed = items.filter((i) => i.kind === "confirmed").reduce((s, i) => s + i.cents, 0);
  const estimated = items.filter((i) => i.kind === "estimated").reduce((s, i) => s + i.cents, 0);
  items.sort((a, b) => (a.date < b.date ? 1 : -1));
  return { paid_cents: acc.subscription.paid_total_cents, confirmed_cents: confirmed, estimated_cents: estimated, found_cents: confirmed + estimated, items };
}
