import type {
  Ask,
  AskDetail,
  CardBurn,
  CardInput,
  CardsResponse,
  CardView,
  Checkout,
  ClockAction,
  ClockResult,
  InboxMessage,
  LedgerEvent,
  Me,
  Meter,
  NotificationPrefs,
  Outcome,
  PlansResponse,
  Quiz,
  QuizAnswers,
  Refinance,
  Rehearsal,
  Result,
  ScheduleInput,
  Sprint,
  Statement,
  StatementFields,
  Subscription,
  Today,
} from "./types";

export class ApiError extends Error {
  status: number;
  code: string;
  details?: Record<string, unknown>;
  constructor(status: number, code: string, message: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/** Text to show a person when a call fails. Server wording wins; network failures get a plain line. */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return "We couldn't reach the server. Check your connection and try again.";
}

function idempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function toError(res: Response): Promise<ApiError> {
  let code = "http_error";
  let message = res.status >= 500 ? "Something went wrong on our side. Please try again." : "That didn't work. Please try again.";
  let details: Record<string, unknown> | undefined;
  try {
    const data = await res.json();
    if (data && data.error) {
      code = data.error.code || code;
      message = data.error.message || message;
      details = data.error.details;
    }
  } catch {
    /* not JSON: keep the generic line */
  }
  return new ApiError(res.status, code, message, details);
}

type Body = Record<string, unknown> | FormData | undefined;

async function send(method: string, path: string, body?: Body, signal?: AbortSignal): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  if (method !== "GET") headers["Idempotency-Key"] = idempotencyKey();
  return fetch(`/api${path}`, { method, headers, body: payload, credentials: "include", signal });
}

let renewing: Promise<unknown> | null = null;

/** One request. A 401 no_session (expired cookie) gets one quiet re-session and one retry. */
async function request<T>(method: string, path: string, body?: Body, signal?: AbortSignal): Promise<T> {
  let res = await send(method, path, body, signal);
  if (res.status === 401 && path !== "/session") {
    renewing ??= send("POST", "/session").finally(() => {
      renewing = null;
    });
    await renewing;
    res = await send(method, path, body, signal);
  }
  if (!res.ok) throw await toError(res);
  if (res.status === 202 || res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(path: string, signal?: AbortSignal) => request<T>("GET", path, undefined, signal);
const post = <T>(path: string, body?: Body) => request<T>("POST", path, body ?? {});
const put = <T>(path: string, body?: Body) => request<T>("PUT", path, body ?? {});

/** Reads a text/event-stream body and calls `onEvent` for every complete event. */
export async function readEventStream(res: Response, onEvent: (event: string, data: string) => void): Promise<void> {
  if (!res.body) throw new ApiError(res.status, "no_stream", "The reply did not arrive. Please try again.");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const flush = (chunk: string) => {
    let event = "message";
    const data: string[] = [];
    for (const line of chunk.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length) onEvent(event, data.join("\n"));
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (value) buf += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buf.search(/\r?\n\r?\n/)) !== -1) {
      const chunk = buf.slice(0, cut);
      buf = buf.slice(cut).replace(/^\r?\n\r?\n/, "");
      flush(chunk);
    }
    if (done) break;
  }
  if (buf.trim()) flush(buf);
}

export const api = {
  // session and account
  session: () => post<Me>("/session"),
  me: () => get<Me>("/me"),
  authLink: (email: string) => post<{ sent: boolean }>("/auth/link", { email }),
  authVerify: (token: string) => post<Me>("/auth/verify", { token }),
  logout: () => post<Record<string, never>>("/auth/logout"),

  // demo
  demoStart: () => post<Me>("/demo/start", { persona: "aziz" }),
  demoClock: (action: ClockAction) => post<ClockResult>("/demo/clock", { action }),
  inbox: () => get<{ messages: InboxMessage[] }>("/inbox"),

  // quiz
  quiz: () => get<Quiz>("/quiz"),
  quizAnswers: (answers: QuizAnswers) => put<Quiz>("/quiz/answers", { answers }),
  quizCards: (cards: CardInput[]) => put<Quiz>("/quiz/cards", { cards }),
  quizSchedule: (schedule: ScheduleInput) => put<Quiz>("/quiz/schedule", { ...schedule }),
  quizSample: () => post<Quiz>("/quiz/sample"),
  cardBurn: (balance_cents: number, apr_bps: number) => post<CardBurn>("/quiz/card-burn", { balance_cents, apr_bps }),
  makeResult: () => post<Result>("/quiz/result"),
  result: () => get<Result>("/quiz/result"),

  // billing
  plans: () => get<PlansResponse>("/billing/plans"),
  checkout: (plan_id: string, email: string) => post<Checkout>("/billing/checkout", { plan_id, email, consent_renewal: true }),
  pay: (id: string, card: { card_number: string; exp: string; cvc: string; name?: string }) =>
    post<Me>(`/billing/checkout/${encodeURIComponent(id)}/pay`, card),
  subscription: () => get<Subscription>("/billing/subscription"),
  cancel: (reason?: string) => post<Subscription>("/billing/cancel", reason ? { reason } : {}),
  resume: () => post<Subscription>("/billing/resume"),
  pause: (weeks: number) => post<Subscription>("/billing/pause", { weeks }),
  refund: () => post<Subscription>("/billing/refund"),

  // today and plan
  today: () => get<Today>("/today"),
  markPaid: (id: string, amount_cents?: number) =>
    post<Today>(`/plan/instructions/${encodeURIComponent(id)}/paid`, amount_cents === undefined ? {} : { amount_cents }),
  undoPaid: (id: string) => post<Today>(`/plan/instructions/${encodeURIComponent(id)}/undo`),
  short: (id: string, available_cents: number) =>
    post<Today>(`/plan/instructions/${encodeURIComponent(id)}/short`, { available_cents }),

  // cards
  cards: () => get<CardsResponse>("/cards"),
  addCard: (card: CardInput) => post<CardView>("/cards", { ...card }),
  patchCard: (id: string, patch: Partial<Pick<CardView, "name" | "last4" | "due_day" | "close_day" | "autopay">> & { statement_min_cents?: number }) =>
    request<CardView>("PATCH", `/cards/${encodeURIComponent(id)}`, patch),
  deleteCard: (id: string) => request<{ ok: boolean }>("DELETE", `/cards/${encodeURIComponent(id)}`),
  addCharge: (id: string, charge: { amount_cents: number; date?: string; note?: string }) =>
    post<CardView>(`/cards/${encodeURIComponent(id)}/charges`, charge),
  ledger: (id: string) => get<{ events: LedgerEvent[] }>(`/cards/${encodeURIComponent(id)}/ledger`),
  refinance: (id: string, monthly_payment_cents?: number, signal?: AbortSignal) =>
    get<Refinance>(
      `/cards/${encodeURIComponent(id)}/refinance${monthly_payment_cents ? `?monthly_payment_cents=${monthly_payment_cents}` : ""}`,
      signal,
    ),

  // statements
  statements: () => get<{ statements: Statement[] }>("/statements"),
  statement: (id: string) => get<Statement>(`/statements/${encodeURIComponent(id)}`),
  uploadStatement: (card_id: string, source: { file: File } | { text: string }) => {
    const form = new FormData();
    form.set("card_id", card_id);
    if ("file" in source) form.set("file", source.file);
    else form.set("text", source.text);
    return post<Statement>("/statements", form);
  },
  manualStatement: (card_id: string, fields: StatementFields) => post<Statement>("/statements/manual", { card_id, fields }),
  confirmStatement: (id: string, fields: StatementFields) =>
    post<Statement>(`/statements/${encodeURIComponent(id)}/confirm`, { fields }),

  // asks
  asks: () => get<{ asks: Ask[]; sprint: Sprint | null }>("/asks"),
  ask: (id: string) => get<AskDetail>(`/asks/${encodeURIComponent(id)}`),
  askOutcome: (id: string, outcome: Outcome) =>
    post<{ ask: Ask; today: Today }>(`/asks/${encodeURIComponent(id)}/outcome`, { ...outcome }),
  askSkip: (id: string) => post<Ask>(`/asks/${encodeURIComponent(id)}/skip`),

  // rehearsal
  rehearsalStart: (ask_id: string) => post<Rehearsal>("/rehearsal", { ask_id }),
  rehearsal: (id: string) => get<Rehearsal>(`/rehearsal/${encodeURIComponent(id)}`),
  /** Sends one line to the simulated rep. `onDelta` gets the reply as it streams; resolves with the final state. */
  rehearsalTurn: async (id: string, text: string, onDelta: (text: string) => void): Promise<Rehearsal> => {
    const res = await fetch(`/api/rehearsal/${encodeURIComponent(id)}/turn`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", "Idempotency-Key": idempotencyKey() },
      body: JSON.stringify({ text }),
      credentials: "include",
    });
    if (!res.ok) throw await toError(res);
    let final: Rehearsal | null = null;
    await readEventStream(res, (event, data) => {
      if (event === "delta") {
        try {
          const piece = JSON.parse(data) as { text?: string };
          if (piece.text) onDelta(piece.text);
        } catch {
          /* skip a malformed chunk */
        }
      } else if (event === "done") {
        final = JSON.parse(data) as Rehearsal;
      } else if (event === "error") {
        let message = "The practice rep stopped answering. Please try again.";
        try {
          message = (JSON.parse(data) as { message?: string }).message || message;
        } catch {
          /* keep the generic line */
        }
        throw new ApiError(502, "stream_error", message);
      }
    });
    if (!final) return api.rehearsal(id);
    return final;
  },

  // other
  meter: () => get<Meter>("/meter"),
  prefs: () => get<NotificationPrefs>("/notifications/prefs"),
  savePrefs: (prefs: Pick<NotificationPrefs, "phone" | "sms_opt_in" | "email_opt_in" | "timezone">) =>
    put<NotificationPrefs>("/notifications/prefs", { ...prefs }),
  /** First-party product events. Names and step ids only: never amounts or anything typed by the user. */
  track: (name: string, props?: Record<string, string | number | boolean>) => {
    void post<void>("/events", { events: [{ name, props, ts: new Date().toISOString() }] }).catch(() => undefined);
  },
};
