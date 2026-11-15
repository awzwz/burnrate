// Shapes from docs/API.md. Money is integer cents, rates are basis points,
// burn_* values are cents per day as floats, dates are YYYY-MM-DD.

export type ISODate = string;
export type Timestamp = string;

export interface Plan {
  id: string;
  name: string;
  price_cents: number;
  days: number;
  renews_price_cents: number;
  renews_days: number;
  per_day_cents: number;
  then: string;
  tag: string | null;
}

export interface Subscription {
  status: "active" | "canceled" | "paused" | "past_due" | "refunded";
  plan: Plan;
  started_at: Timestamp;
  current_period_end: Timestamp;
  cancel_at_period_end: boolean;
  paused_until: ISODate | null;
  renews_price_cents: number;
  paid_total_cents: number;
  refundable_until: ISODate | null;
}

export interface Me {
  user: { id: string; anonymous: boolean; email: string | null; demo: boolean };
  subscription: Subscription | null;
  entitled: boolean;
  today: ISODate;
  demo: { persona: string; started_on: ISODate; today: ISODate; real_today: ISODate } | null;
  config: {
    billing_provider: "demo" | "stripe";
    test_mode: boolean;
    test_card: string;
    llm_provider: "openai" | "anthropic" | "fake";
    sms_live: boolean;
  };
}

export type ClockAction = "next_payday" | "next_statement" | "plus_day" | "reset";
export interface ClockResult {
  me: Me;
  jumped_to: ISODate;
  note: string;
}

export interface InboxMessage {
  id: string;
  channel: "sms" | "email";
  to: string;
  subject: string | null;
  body: string;
  link: string | null;
  sent_at: Timestamp;
}

// ---------- quiz ----------
export interface CardInput {
  id?: string;
  name: string;
  issuer?: string;
  balance_cents: number;
  apr_bps: number;
  statement_min_cents?: number | null;
  due_day: number;
  close_day?: number;
}

export type Frequency = "weekly" | "biweekly" | "semimonthly" | "monthly";
export interface ScheduleInput {
  frequency: Frequency;
  anchor: ISODate;
  per_payday_cents: number;
  second_day?: number;
}

export type QuizAnswers = Record<string, unknown>;
export interface Quiz {
  answers: QuizAnswers;
  cards: CardInput[];
  schedule: ScheduleInput | null;
  has_result: boolean;
}

export interface CardBurn {
  burn_per_day: number;
  per_year_cents: number;
}

export interface Projection {
  strategy: string;
  months: number;
  interest_cents: number;
  never: boolean;
  debt_free_month: string | null;
}

export interface FirstMove {
  payday: ISODate;
  amount_cents: number;
  card_id: string;
  card_name: string;
  covers_min: boolean;
  min_cents: number;
  extra_cents: number;
  saved_by_date_cents: number;
  days_early: number;
  burn_after: number;
  strategy: string;
  reason: string;
  short_by_cents: number;
}

export interface ResultCard {
  id: string;
  name: string;
  issuer: string;
  balance_cents: number;
  apr_bps: number;
  burn_per_day: number;
  min_payment_cents: number;
  next_due: ISODate;
}

export interface AskPreview {
  type: string;
  title: string;
  detail: string;
  value_label: string;
}

export interface Result {
  id: string;
  created_at: Timestamp;
  engine_version: string;
  today: ISODate;
  burn_per_day: number;
  per_paycheck_cents: number;
  paycheck_days: number;
  per_month_cents: number;
  per_year_cents: number;
  total_balance_cents: number;
  cards: ResultCard[];
  first_move: FirstMove | null;
  payoff: { monthly_budget_cents: number; plan: Projection; minimums: Projection };
  point_value: { card_id: string; card_name: string; per_point_per_year_cents: number };
  asks: AskPreview[];
  assumptions: string[];
}

// ---------- billing ----------
export interface PlansResponse {
  plans: Plan[];
  guarantee_days: number;
  provider: string;
  test_mode: boolean;
  test_card: string;
}

export interface Checkout {
  id: string;
  provider: string;
  url: string | null;
  plan: Plan;
  amount_cents: number;
  test_mode: boolean;
  test_card: string;
  status: string;
}

// ---------- product ----------
export interface Send {
  card_id: string;
  card_name: string;
  issuer: string;
  last4: string;
  apr_bps: number;
  amount_cents: number;
  covers_min: boolean;
  min_cents: number;
  extra_cents: number;
  due: ISODate;
  days_early: number;
  saved_by_date_cents: number;
  burn_cut: number;
  pay_url: string;
}

export interface Instruction {
  id: string;
  payday: ISODate;
  next_payday: ISODate;
  status: "due" | "paid" | "short" | "upcoming";
  sends: Send[];
  reserved_cents: number;
  short_by_cents: number;
  strategy: string;
  reason: string;
  burn_before: number;
  burn_after: number;
  total_send_cents: number;
  paid_at: Timestamp | null;
  paid_cents: number | null;
}

export interface MeterItem {
  label: string;
  cents: number;
  kind: "confirmed" | "estimated";
  date: ISODate;
}
export interface Meter {
  paid_cents: number;
  confirmed_cents: number;
  estimated_cents: number;
  found_cents: number;
  items: MeterItem[];
}

export interface CardView {
  id: string;
  name: string;
  issuer: string;
  last4: string;
  balance_cents: number;
  apr_bps: number;
  burn_per_day: number;
  min_payment_cents: number;
  due_day: number;
  close_day: number;
  next_due: ISODate;
  autopay: string;
  pay_url: string;
  start_balance_cents: number;
  progress: number;
  paid_off: boolean;
  min_rule_verified: boolean;
}

export interface BurnPoint {
  date: ISODate;
  value: number;
  label: string;
  delta: number;
}

export interface AutopayRow {
  card_id: string;
  card_name: string;
  issuer: string;
  min_cents: number;
  due: ISODate;
  source: "this_paycheck" | "next_paycheck" | "covered_by_payment";
  note: string;
}

export interface Sprint {
  done: number;
  total: number;
  ends_on: ISODate;
}

export type AskType = "late_fee_refund" | "apr_reduction" | "annual_fee" | "due_date_move" | "hardship";
export interface Ask {
  id: string;
  type: AskType;
  card_id: string;
  card_name: string;
  issuer: string;
  title: string;
  subtitle: string;
  value_cents: number | null;
  value_label: string;
  status: "queued" | "done" | "skipped";
  order: number;
  result: string | null;
}

export interface CallNode {
  say: string;
  hint?: string;
  options: { label: string; next: string }[];
  outcome?: string;
}

export interface AskDetail extends Ask {
  why: string;
  evidence: { text: string; source: string; caveat: string };
  script: { opening: string; lines: { label: string; text: string }[]; must_ask: string[]; if_refused: string };
  written: string;
  call_card: { start: string; nodes: Record<string, CallNode> };
  playbook: { count: number; entries: { channel: string; result: string; note: string; month: string }[] };
}

export type OutcomeResult = "granted" | "partial" | "temporary" | "refused" | "no_answer";
export interface Outcome {
  result: OutcomeResult;
  channel: "phone" | "chat" | "message";
  new_apr_bps?: number;
  until?: ISODate;
  refund_cents?: number;
  rep_name?: string;
  hard_pull?: "yes" | "no" | "unknown";
  ask_again_on?: ISODate;
  note?: string;
}

export interface Upcoming {
  date: ISODate;
  kind: "payday" | "statement_close" | "due";
  card_id?: string;
  title: string;
}

export interface Today {
  date: ISODate;
  headline: string;
  lead: string;
  instruction: Instruction | null;
  burn: { now: number; start: number; down_by: number; started_on: ISODate; history: BurnPoint[] };
  autopay: AutopayRow[];
  next_ask: Ask | null;
  sprint: Sprint | null;
  meter: Meter;
  cards: CardView[];
  upcoming: Upcoming[];
}

export interface CardsResponse {
  cards: CardView[];
  burn_per_day: number;
  total_balance_cents: number;
}

export interface LedgerEvent {
  id: string;
  type: string;
  amount_cents: number;
  signed_cents: number;
  date: ISODate;
  source: string;
  note: string | null;
}

export interface TransferOption {
  fee_cents: number;
  breakeven_months: number | null;
  transfer_cost_cents: number;
  transfer_months: number;
  saving_cents: number;
  clears_in_promo: boolean;
  payment_to_clear_in_promo_cents: number;
}

export interface Refinance {
  stay: { months: number; interest_cents: number };
  transfer: { promo_months: number; fee_3: TransferOption; fee_5: TransferOption };
  loans: {
    loan_apr_bps: number;
    origination_bps: number;
    payment_cents: number;
    loan_cost_cents: number;
    cards_cost_cents: number;
    saving_cents: number;
  }[];
  note: string;
}

export interface StatementFields {
  new_balance_cents?: number | null;
  previous_balance_cents?: number | null;
  payments_credits_cents?: number | null;
  purchases_cents?: number | null;
  fees_cents?: number | null;
  interest_cents?: number | null;
  apr_bps?: number | null;
  balance_subject_to_interest_cents?: number | null;
  days_in_cycle?: number | null;
  min_payment_cents?: number | null;
  close_date?: ISODate | null;
  due_date?: ISODate | null;
  late_fee_cents?: number | null;
  interest_ytd_cents?: number | null;
}
export type StatementFieldName = keyof StatementFields;

export interface Statement {
  id: string;
  card_id: string;
  card_name: string;
  status: "extracting" | "needs_confirmation" | "reconciled" | "failed";
  source: "upload" | "manual" | "demo";
  fields: StatementFields;
  checks: { name: string; ok: boolean | null; detail: string }[];
  confirm_fields: string[];
  reconciliation: {
    predicted_interest_cents: number;
    actual_interest_cents: number;
    diff_cents: number;
    parts: { label: string; cents: number }[];
    summary: string;
    within_tolerance: boolean;
  } | null;
  triggers: { type: string; title: string; detail: string }[];
  error: string | null;
  created_at: Timestamp;
  close_date: ISODate | null;
  due_date: ISODate | null;
}

export interface Rehearsal {
  id: string;
  ask_id: string;
  scenario: string;
  provider: string;
  finished: boolean;
  messages: { role: "rep" | "you"; text: string }[];
  checklist: { id: string; label: string; done: boolean }[];
}

export interface NotificationPrefs {
  phone: string | null;
  sms_opt_in: boolean;
  email_opt_in: boolean;
  timezone: string;
  calendar_url?: string | null;
}
