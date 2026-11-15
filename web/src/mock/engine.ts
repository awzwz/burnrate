// In-browser stand-in for the server's engine. Used only when the app runs with VITE_API_MOCK=1.
// It is deliberately simple arithmetic (daily simple interest, monthly projections): enough to keep
// the sample persona's numbers consistent while every screen is clicked through.
import type { Frequency, Instruction, Projection, Refinance, ScheduleInput, TransferOption } from "../api/types";

const DAY = 86400000;

export function toDate(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}
export function toISO(d: Date): string {
  return d.toISOString().slice(0, 10);
}
export function addDays(iso: string, n: number): string {
  return toISO(new Date(toDate(iso).getTime() + n * DAY));
}
export function diffDays(a: string, b: string): number {
  return Math.round((toDate(b).getTime() - toDate(a).getTime()) / DAY);
}
export function realToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function daysInMonth(y: number, m0: number): number {
  return new Date(Date.UTC(y, m0 + 1, 0)).getUTCDate();
}
function dayInMonth(y: number, m0: number, day: number): string {
  return toISO(new Date(Date.UTC(y, m0, Math.min(day, daysInMonth(y, m0)))));
}
/** First date on or after `from` whose day of month is `day` (clamped to the month length). */
export function nextDayOfMonth(day: number, from: string): string {
  const f = toDate(from);
  let y = f.getUTCFullYear();
  let m = f.getUTCMonth();
  for (let i = 0; i < 3; i++) {
    const candidate = dayInMonth(y, m, day);
    if (candidate >= from) return candidate;
    m += 1;
    if (m > 11) {
      m = 0;
      y += 1;
    }
  }
  return from;
}
export function addMonths(iso: string, n: number): string {
  const d = toDate(iso);
  const total = d.getUTCFullYear() * 12 + d.getUTCMonth() + n;
  return dayInMonth(Math.floor(total / 12), total % 12, d.getUTCDate());
}

// ---------- cards ----------
export interface EngineCard {
  id: string;
  name: string;
  issuer: string;
  balance_cents: number;
  apr_bps: number;
  due_day: number;
  close_day: number;
  statement_min_cents: number | null;
}

/** Cents per day. */
export const burnOf = (c: { balance_cents: number; apr_bps: number }) => (Math.max(0, c.balance_cents) * c.apr_bps) / 10000 / 365;
export const totalBurn = (cards: { balance_cents: number; apr_bps: number }[]) => cards.reduce((s, c) => s + burnOf(c), 0);
export const round4 = (x: number) => Math.round(x * 10000) / 10000;

export function minPayment(c: EngineCard): number {
  if (c.balance_cents <= 0) return 0;
  const interest = burnOf(c) * 30;
  let m: number;
  // The minimum printed on the last statement wins; otherwise the issuer's formula.
  if (c.statement_min_cents !== null) m = c.statement_min_cents;
  else if (c.issuer === "chase") m = Math.max(3500, 0.01 * c.balance_cents + interest);
  else if (c.issuer === "discover") m = Math.ceil(Math.max(3500, 0.02 * c.balance_cents, 2000 + interest) / 100) * 100;
  else m = Math.max(2500, 0.01 * c.balance_cents + interest);
  return Math.min(Math.round(m), c.balance_cents);
}

const ISSUER_WORDS: [string, string][] = [
  ["capital one", "capital_one"], ["quicksilver", "capital_one"], ["venture", "capital_one"], ["savor", "capital_one"],
  ["chase", "chase"], ["freedom", "chase"], ["sapphire", "chase"], ["discover", "discover"], ["citi", "citi"],
  ["amex", "amex"], ["american express", "amex"], ["bank of america", "bank_of_america"], ["bofa", "bank_of_america"],
  ["wells fargo", "wells_fargo"],
];
export function guessIssuer(name: string): string {
  const low = name.toLowerCase();
  return ISSUER_WORDS.find(([k]) => low.includes(k))?.[1] ?? "other";
}
export const PAY_URLS: Record<string, string> = {
  capital_one: "https://www.capitalone.com", chase: "https://www.chase.com", discover: "https://www.discover.com",
  citi: "https://www.citi.com", amex: "https://www.americanexpress.com", bank_of_america: "https://www.bankofamerica.com",
  wells_fargo: "https://www.wellsfargo.com",
};
export const ISSUER_NAMES: Record<string, string> = {
  capital_one: "Capital One", chase: "Chase", discover: "Discover", citi: "Citi", amex: "American Express",
  bank_of_america: "Bank of America", wells_fargo: "Wells Fargo",
};
export const issuerName = (c: { issuer: string; name: string }) => ISSUER_NAMES[c.issuer] ?? c.name;
/** "Capital One Quicksilver" -> "Quicksilver" for short event labels. */
export function shortName(c: { issuer: string; name: string }): string {
  const brand = ISSUER_NAMES[c.issuer];
  if (brand && c.name.toLowerCase().startsWith(brand.toLowerCase() + " ")) {
    const rest = c.name.slice(brand.length + 1).trim();
    if (rest.length > 3) return rest;
  }
  return c.name;
}

// ---------- pay schedule ----------
export const PAYDAYS_PER_YEAR: Record<Frequency, number> = { weekly: 52, biweekly: 26, semimonthly: 24, monthly: 12 };
export const PAYCHECK_DAYS: Record<Frequency, number> = { weekly: 7, biweekly: 14, semimonthly: 15, monthly: 30 };

/** The first `count` paydays on or after `from`. */
export function paydays(s: ScheduleInput, from: string, count: number): string[] {
  const out: string[] = [];
  if (s.frequency === "weekly" || s.frequency === "biweekly") {
    const step = s.frequency === "weekly" ? 7 : 14;
    const k = Math.ceil(diffDays(s.anchor, from) / step);
    for (let i = 0; i < count; i++) out.push(addDays(s.anchor, (k + i) * step));
    return out;
  }
  const anchorDay = toDate(s.anchor).getUTCDate();
  const days = s.frequency === "semimonthly" ? [...new Set([anchorDay, s.second_day ?? 15])].sort((a, b) => a - b) : [anchorDay];
  const f = toDate(from);
  let y = f.getUTCFullYear();
  let m = f.getUTCMonth();
  while (out.length < count) {
    for (const d of days) {
      const candidate = dayInMonth(y, m, d);
      if (candidate >= from && out.length < count) out.push(candidate);
    }
    m += 1;
    if (m > 11) {
      m = 0;
      y += 1;
    }
  }
  return out;
}

// ---------- the payday instruction ----------
export function buildInstruction(
  cards: EngineCard[],
  schedule: ScheduleInput,
  payday: string,
  extra: { last4?: Record<string, string>; available_cents?: number } = {},
): Omit<Instruction, "id" | "status" | "paid_at" | "paid_cents"> | null {
  const live = cards.filter((c) => c.balance_cents > 0);
  if (!live.length) return null;
  const next_payday = paydays(schedule, addDays(payday, 1), 1)[0];
  const target = [...live].sort((a, b) => b.apr_bps - a.apr_bps || b.balance_cents - a.balance_cents)[0];
  const rows = live.map((c) => ({ c, min: minPayment(c), due: nextDayOfMonth(c.due_day, payday) }));
  const reserved = rows.filter((r) => r.c.id !== target.id && r.due < next_payday).reduce((s, r) => s + r.min, 0);
  const planned = Math.min(Math.max(0, schedule.per_payday_cents - reserved), target.balance_cents);
  const budget = extra.available_cents ?? schedule.per_payday_cents;
  const amount = Math.min(Math.max(0, budget - reserved), target.balance_cents);
  const tmin = minPayment(target);
  const due = nextDayOfMonth(target.due_day, payday);
  const days_early = Math.max(0, diffDays(payday, due));
  const burn_cut = (amount * target.apr_bps) / 10000 / 365;
  const burn_before = totalBurn(live);
  const highest = live.every((c) => c.apr_bps <= target.apr_bps);
  return {
    payday,
    next_payday,
    sends: [
      {
        card_id: target.id,
        card_name: target.name,
        issuer: target.issuer,
        last4: extra.last4?.[target.id] ?? "",
        apr_bps: target.apr_bps,
        amount_cents: amount,
        covers_min: amount >= tmin,
        min_cents: tmin,
        extra_cents: Math.max(0, amount - tmin),
        due,
        days_early,
        saved_by_date_cents: Math.round(burn_cut * days_early),
        burn_cut: round4(burn_cut),
        pay_url: PAY_URLS[target.issuer] ?? "",
      },
    ],
    reserved_cents: reserved,
    short_by_cents: Math.max(0, planned - amount),
    strategy: "avalanche",
    reason: highest ? `${target.name} has your highest rate.` : `${target.name} is the card to clear first.`,
    burn_before: round4(burn_before),
    burn_after: round4(burn_before - burn_cut),
    total_send_cents: amount,
  };
}

// ---------- projections ----------
function project(cards: EngineCard[], monthlyBudget: number | null, startISO: string, strategy: string): Projection {
  const bal = cards.map((c) => c.balance_cents);
  let interestTotal = 0;
  let months = 0;
  const order = cards.map((_, i) => i).sort((a, b) => cards[b].apr_bps - cards[a].apr_bps);
  while (bal.some((b) => b > 0.5) && months < 600) {
    months += 1;
    const mins: number[] = [];
    bal.forEach((b, i) => {
      if (b <= 0) {
        mins.push(0);
        return;
      }
      const interest = (b * cards[i].apr_bps) / 10000 / 12;
      interestTotal += interest;
      bal[i] = b + interest;
      mins.push(Math.min(bal[i], Math.max(3500, 0.01 * b + interest)));
    });
    let pool = monthlyBudget === null ? 0 : Math.max(0, monthlyBudget - mins.reduce((s, x) => s + x, 0));
    bal.forEach((_, i) => {
      bal[i] -= mins[i];
    });
    for (const i of order) {
      if (pool <= 0) break;
      const pay = Math.min(pool, bal[i]);
      bal[i] -= pay;
      pool -= pay;
    }
  }
  const never = months >= 600;
  return {
    strategy,
    months,
    interest_cents: Math.round(interestTotal),
    never,
    debt_free_month: never ? null : addMonths(startISO, months).slice(0, 7),
  };
}
export const projectPlan = (cards: EngineCard[], budget: number, start: string) => project(cards, budget, start, "avalanche");
export const projectMinimums = (cards: EngineCard[], start: string) => project(cards, null, start, "minimums");

// ---------- refinance comparison for one card ----------
function payDown(balance: number, aprBps: number, payment: number, maxMonths = 600): { months: number; interest: number; left: number } {
  let b = balance;
  let interest = 0;
  let months = 0;
  while (b > 0.5 && months < maxMonths) {
    const i = (b * aprBps) / 10000 / 12;
    interest += i;
    b = b + i - Math.min(payment, b + i);
    months += 1;
  }
  return { months, interest, left: b };
}

export function refinance(card: EngineCard, monthlyPayment: number): Refinance {
  const B = card.balance_cents;
  const P = Math.max(monthlyPayment, 100);
  const stay = payDown(B, card.apr_bps, P);
  const promo = 18;
  const transfer = (feeBps: number): TransferOption => {
    const fee = Math.round((B * feeBps) / 10000);
    const start = B + fee;
    const inPromo = Math.min(start, P * promo);
    const left = start - inPromo;
    const after = left > 0 ? payDown(left, card.apr_bps, P) : { months: 0, interest: 0, left: 0 };
    const cost = fee + Math.round(after.interest);
    let cumulative = 0;
    let breakeven: number | null = null;
    let b = B;
    for (let m = 1; m <= 120 && b > 0.5; m++) {
      const i = (b * card.apr_bps) / 10000 / 12;
      cumulative += i;
      b = b + i - Math.min(P, b + i);
      if (cumulative >= fee) {
        breakeven = m;
        break;
      }
    }
    return {
      fee_cents: fee,
      breakeven_months: breakeven,
      transfer_cost_cents: cost,
      transfer_months: left > 0 ? promo + after.months : Math.ceil(start / P),
      saving_cents: Math.round(stay.interest) - cost,
      clears_in_promo: left <= 0,
      payment_to_clear_in_promo_cents: Math.ceil(start / promo),
    };
  };
  const loan = (aprBps: number, originationBps: number) => {
    const fee = Math.round((B * originationBps) / 10000);
    const principal = B + fee;
    const r = aprBps / 10000 / 12;
    const n = 36;
    const payment = Math.round((principal * r) / (1 - Math.pow(1 + r, -n)));
    const cost = payment * n - B;
    return {
      loan_apr_bps: aprBps,
      origination_bps: originationBps,
      payment_cents: payment,
      loan_cost_cents: cost,
      cards_cost_cents: Math.round(stay.interest),
      saving_cents: Math.round(stay.interest) - cost,
    };
  };
  return {
    stay: { months: stay.months, interest_cents: Math.round(stay.interest) },
    transfer: { promo_months: promo, fee_3: transfer(300), fee_5: transfer(500) },
    loans: [loan(1499, 500), loan(2199, 800)],
    note: "Example terms, not offers. A transfer or a loan only costs less if this card stays unused afterwards and the new payment is made every month.",
  };
}
