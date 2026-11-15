import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../../api/client";
import type { CardInput, Frequency, Quiz as QuizState, ScheduleInput } from "../../api/types";
import { ErrorBox, Loading, Plastic } from "../../components/Bits";
import FunnelTop from "../../components/FunnelTop";
import { Chevron } from "../../components/Icons";
import { bpsToInput, centsToInput, localISODate, ordinal, parseAprToBps, parseMoneyToCents, usdPerDay } from "../../lib/format";
import { useLoad } from "../../lib/useLoad";
import { quizStart } from "../../lib/useTicker";
import { useSession } from "../../state/session";

// ---------- step model ----------
const COUNT_TO_N: Record<string, number> = { "1": 1, "2": 2, "3": 3, "4+": 4 };
const str = (v: unknown): string => (typeof v === "string" ? v : "");

export function plannedCards(q: QuizState): number {
  const explicit = Number(q.answers.cards_planned);
  const base = Number.isInteger(explicit) && explicit > 0 ? explicit : (COUNT_TO_N[str(q.answers.card_count)] ?? 3);
  return Math.min(6, Math.max(1, base));
}

export function quizSteps(q: QuizState): string[] {
  const cards = Array.from({ length: plannedCards(q) }, (_, i) => `card-${i + 1}`);
  return ["count", "age", "carry", ...cards, "frequency", "payday", "amount", "late-fee", "autopay"];
}

function frequencyOf(q: QuizState): Frequency | "" {
  const f = str(q.answers.pay_frequency) || q.schedule?.frequency || "";
  return f === "weekly" || f === "biweekly" || f === "semimonthly" || f === "monthly" ? f : "";
}
function paydayOf(q: QuizState, today: string): string {
  const d = str(q.answers.next_payday) || q.schedule?.anchor || "";
  return d && d >= today ? d : "";
}

/** The first step that still needs an answer, or null when the quiz is complete. */
export function firstOpenStep(q: QuizState, today: string): string | null {
  const a = q.answers;
  if (!str(a.card_count)) return "count";
  if (!str(a.age_band)) return "age";
  if (!str(a.carry_months)) return "carry";
  if (q.cards.length < plannedCards(q)) return `card-${q.cards.length + 1}`;
  if (!frequencyOf(q)) return "frequency";
  if (!paydayOf(q, today)) return "payday";
  if (!q.schedule) return "amount";
  if (typeof a.late_fee_recently !== "boolean") return "late-fee";
  if (!str(a.autopay)) return "autopay";
  return null;
}

// ---------- shared step chrome ----------
interface StepProps {
  quiz: QuizState;
  today: string;
  /** Runs the save; on success the quiz moves to the next step. */
  advance: (save: () => Promise<QuizState | void>, then?: "next" | "result" | string) => Promise<void>;
  busy: boolean;
  error: string | null;
  setError: (e: string | null) => void;
  setQuiz: (q: QuizState) => void;
}

function Shell({ title, sub, children, cta, busy, error, onSubmit, extra }: { title: string; sub?: string; children: ReactNode; cta: string; busy: boolean; error: string | null; onSubmit: () => void; extra?: ReactNode }) {
  const h = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    h.current?.focus({ preventScroll: true });
  }, []);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!busy) onSubmit();
  };
  return (
    <form onSubmit={submit} noValidate style={{ display: "contents" }}>
      <main className="body">
        <h1 className="title" tabIndex={-1} ref={h} style={{ outline: "none" }}>
          {title}
        </h1>
        {sub && <p className="sub">{sub}</p>}
        {children}
        {error && (
          <p className="err center" role="alert">
            {error}
          </p>
        )}
        {extra}
      </main>
      <div className="cta-wrap">
        <button className="cta" type="submit" disabled={busy} data-testid="quiz-next">
          {busy ? "Saving" : cta}
        </button>
      </div>
    </form>
  );
}

function Options({ name, legend, value, onChange, options }: { name: string; legend: string; value: string; onChange: (v: string) => void; options: { value: string; label: string; detail?: string; right?: string }[] }) {
  return (
    <fieldset className="opts">
      <legend className="sr">{legend}</legend>
      {options.map((o) => (
        <label className="opt" key={o.value}>
          <input type="radio" name={name} value={o.value} checked={value === o.value} onChange={() => onChange(o.value)} data-testid={`quiz-option-${o.value}`} />
          <span>{o.label}</span>
          {o.right && <span className="r">{o.right}</span>}
          {o.detail && <span className="d">{o.detail}</span>}
        </label>
      ))}
    </fieldset>
  );
}

function ChoiceStep({ p, answerKey, title, sub, options, cta = "Continue", extraAnswers }: { p: StepProps; answerKey: string; title: string; sub?: string; options: { value: string; label: string; detail?: string; right?: string }[]; cta?: string; extraAnswers?: (v: string) => Record<string, unknown> }) {
  const [value, setValue] = useState(str(p.quiz.answers[answerKey]));
  const submit = () => {
    if (!value) return p.setError("Choose one to continue.");
    void p.advance(() => api.quizAnswers({ [answerKey]: value, ...(extraAnswers ? extraAnswers(value) : {}) }));
  };
  return (
    <Shell title={title} sub={sub} cta={cta} busy={p.busy} error={p.error} onSubmit={submit}>
      <Options
        name={answerKey}
        legend={title}
        value={value}
        onChange={(v) => {
          setValue(v);
          p.setError(null);
        }}
        options={options}
      />
    </Shell>
  );
}

// ---------- step: how many cards ----------
function CountStep(p: StepProps) {
  const [value, setValue] = useState(str(p.quiz.answers.card_count));
  const submit = () => {
    if (!value) return p.setError("Choose one to continue.");
    const n = COUNT_TO_N[value] ?? 3;
    void p.advance(async () => {
      let q = await api.quizAnswers({ card_count: value, cards_planned: n });
      // Fewer cards than already entered: keep the first ones.
      if (q.cards.length > n) q = await api.quizCards(q.cards.slice(0, n));
      return q;
    });
  };
  return (
    <Shell title="How many cards carry a balance?" sub="Count the cards where you don't pay the full statement each month." cta="Continue" busy={p.busy} error={p.error} onSubmit={submit}>
      <Options
        name="card_count"
        legend="How many cards carry a balance?"
        value={value}
        onChange={(v) => {
          setValue(v);
          p.setError(null);
        }}
        options={[
          { value: "1", label: "1 card" },
          { value: "2", label: "2 cards" },
          { value: "3", label: "3 cards" },
          { value: "4+", label: "4 or more" },
        ]}
      />
      <p className="fine center">Burn Rate is a paid plan. Your number is free: you see it before any price.</p>
    </Shell>
  );
}

// ---------- step: one card ----------
const NTH = ["first", "second", "third", "fourth", "fifth", "sixth"];

function StatementSample({ defaultOpen }: { defaultOpen: boolean }) {
  return (
    <details className="where" open={defaultOpen}>
      <summary>
        <Chevron /> Where these are on a statement
      </summary>
      <div className="stmt" role="img" aria-label="Sample statement. New Balance is number 1, the purchase APR is number 2, Minimum Payment Due is number 3 and Payment Due Date is number 4.">
        <div className="bank">
          Account Summary <span>Oct 13 – Nov 11, 2026 · 30 days</span>
        </div>
        <dl>
          <dt>New Balance</dt>
          <dd>
            <mark>$5,200.00</mark>
            <sup>1</sup>
          </dd>
          <dt>Minimum Payment Due</dt>
          <dd>
            <mark>$171.33</mark>
            <sup>3</sup>
          </dd>
          <dt>Payment Due Date</dt>
          <dd>
            <mark>Dec 9, 2026</mark>
            <sup>4</sup>
          </dd>
        </dl>
        <div className="cap">Interest Charge Calculation</div>
        <table>
          <thead>
            <tr>
              <th>
                Type of
                <br />
                Balance
              </th>
              <th>
                Annual Percentage
                <br />
                Rate (APR)
              </th>
              <th>
                Balance Subject to
                <br />
                Interest Rate
              </th>
              <th>
                Interest
                <br />
                Charge
              </th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Purchases</td>
              <td>
                <mark>27.99% (v)</mark>
                <sup>2</sup>
              </td>
              <td>$5,187.20</td>
              <td>$119.33</td>
            </tr>
          </tbody>
        </table>
        <div className="samp">Sample statement. Yours will look a little different.</div>
      </div>
    </details>
  );
}

function CardStep({ p, index }: { p: StepProps; index: number }) {
  const saved: CardInput | undefined = p.quiz.cards[index];
  const planned = plannedCards(p.quiz);
  const [name, setName] = useState(saved?.name ?? "");
  const [balance, setBalance] = useState(saved ? centsToInput(saved.balance_cents) : "");
  const [apr, setApr] = useState(saved ? bpsToInput(saved.apr_bps) : "");
  const [min, setMin] = useState(saved?.statement_min_cents != null ? centsToInput(saved.statement_min_cents) : "");
  const [due, setDue] = useState(saved ? String(saved.due_day) : "");
  const [bad, setBad] = useState<string | null>(null);
  const [live, setLive] = useState<number | null>(null);
  const id = useId();

  const balanceCents = parseMoneyToCents(balance);
  const aprBps = parseAprToBps(apr);

  // The live line: ask the server once typing pauses.
  useEffect(() => {
    if (balanceCents === null || aprBps === null) {
      setLive(null);
      return;
    }
    let liveReq = true;
    const t = window.setTimeout(() => {
      api
        .cardBurn(balanceCents, aprBps)
        .then((r) => liveReq && setLive(r.burn_per_day))
        .catch(() => liveReq && setLive(null));
    }, 350);
    return () => {
      liveReq = false;
      window.clearTimeout(t);
    };
  }, [balanceCents, aprBps]);

  const build = (): CardInput | null => {
    const fail = (field: string, message: string) => {
      setBad(field);
      p.setError(message);
      return null;
    };
    if (!name.trim()) return fail("name", "Give this card a name, for example the one printed on it.");
    if (balanceCents === null) return fail("balance", "Enter the balance as an amount, like 5,200.00.");
    if (aprBps === null) return fail("apr", "Enter the interest rate as a number, like 27.99.");
    let minCents: number | null = null;
    if (min.trim()) {
      minCents = parseMoneyToCents(min);
      if (minCents === null) return fail("min", "Enter the minimum payment as an amount, or leave it empty.");
    }
    if (!due) return fail("due", "Choose the day of the month the payment is due.");
    setBad(null);
    // Keep what the server added (id, issuer, closing day) unless the card was renamed.
    const base = saved && saved.name === name.trim() ? saved : {};
    return { ...base, name: name.trim(), balance_cents: balanceCents, apr_bps: aprBps, statement_min_cents: minCents, due_day: Number(due) };
  };

  const save = (alsoPlan?: number) => {
    const card = build();
    if (!card) return;
    void p.advance(async () => {
      const cards = [...p.quiz.cards];
      cards[index] = card;
      let q = await api.quizCards(cards);
      if (alsoPlan) q = await api.quizAnswers({ cards_planned: alsoPlan });
      return q;
    });
  };

  const useSample = () => {
    void p.advance(() => api.quizSample(), "result");
  };

  const isLast = index + 1 >= planned;
  const clear = () => {
    setBad(null);
    p.setError(null);
  };

  return (
    <Shell
      title={`Your ${NTH[index] ?? "next"} card`}
      sub="Four numbers from the latest statement. No bank login, no card number."
      cta={isLast ? "Continue" : `Add card ${index + 2}`}
      busy={p.busy}
      error={p.error}
      onSubmit={() => save()}
      extra={
        <>
          <button className="textlink quiet block" type="button" onClick={useSample} disabled={p.busy} data-testid="quiz-sample">
            No statement at hand? Use sample numbers
          </button>
          {isLast && planned < 6 && (
            <button className="textlink quiet block" type="button" onClick={() => save(planned + 1)} disabled={p.busy} data-testid="quiz-add-card">
              Save and add another card
            </button>
          )}
        </>
      }
    >
      <div className="fields">
        <div className={`field name ${bad === "name" ? "bad" : ""}`}>
          <Plastic />
          <input
            id={`${id}-name`}
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              clear();
            }}
            placeholder="Card name, for example Chase Freedom"
            aria-label="Card name"
            aria-invalid={bad === "name"}
            autoComplete="off"
            maxLength={60}
            data-testid="card-name"
          />
        </div>
        <div className={`field ${bad === "balance" ? "bad" : ""}`}>
          <i className="n num">1</i>
          <label htmlFor={`${id}-bal`}>Balance</label>
          <div className="in num">
            <span>$</span>
            <input
              id={`${id}-bal`}
              inputMode="decimal"
              value={balance}
              onChange={(e) => {
                setBalance(e.target.value);
                clear();
              }}
              placeholder="0.00"
              aria-invalid={bad === "balance"}
              autoComplete="off"
              data-testid="card-balance"
            />
          </div>
        </div>
        <div className={`field ${bad === "apr" ? "bad" : ""}`}>
          <i className="n num">2</i>
          <label htmlFor={`${id}-apr`}>Interest rate (APR)</label>
          <div className="in num">
            <input
              id={`${id}-apr`}
              inputMode="decimal"
              value={apr}
              onChange={(e) => {
                setApr(e.target.value);
                clear();
              }}
              placeholder="0.00"
              aria-invalid={bad === "apr"}
              autoComplete="off"
              data-testid="card-apr"
            />
            <span>%</span>
          </div>
        </div>
        <div className={`field ${bad === "min" ? "bad" : ""}`}>
          <i className="n num">3</i>
          <label htmlFor={`${id}-min`}>Minimum payment</label>
          <div className="in num">
            <span>$</span>
            <input
              id={`${id}-min`}
              inputMode="decimal"
              value={min}
              onChange={(e) => {
                setMin(e.target.value);
                clear();
              }}
              placeholder="0.00"
              aria-invalid={bad === "min"}
              autoComplete="off"
              data-testid="card-min"
            />
          </div>
        </div>
        <div className={`field ${bad === "due" ? "bad" : ""}`}>
          <i className="n num">4</i>
          <label htmlFor={`${id}-due`}>Payment due, day of month</label>
          <div className="in">
            <select
              id={`${id}-due`}
              value={due}
              onChange={(e) => {
                setDue(e.target.value);
                clear();
              }}
              aria-invalid={bad === "due"}
              data-testid="card-due"
            >
              <option value="">Choose</option>
              {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
                <option key={d} value={d}>
                  {ordinal(d)}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <p className="live" aria-live="polite">
        <span>This card alone costs you</span>
        <b className="num" data-testid="card-live-burn">
          {live === null ? "—" : `${usdPerDay(live)} a day`}
        </b>
      </p>

      <StatementSample defaultOpen={index === 0} />
    </Shell>
  );
}

// ---------- steps: pay schedule ----------
function scheduleFrom(q: QuizState, patch: Partial<ScheduleInput>): ScheduleInput | null {
  const frequency = patch.frequency ?? frequencyOf(q);
  const anchor = patch.anchor ?? (str(q.answers.next_payday) || q.schedule?.anchor);
  const per = patch.per_payday_cents ?? q.schedule?.per_payday_cents;
  if (!frequency || !anchor || per === undefined) return null;
  const second = patch.second_day ?? (Number(q.answers.second_payday_day) || q.schedule?.second_day);
  return { frequency, anchor, per_payday_cents: per, ...(frequency === "semimonthly" && second ? { second_day: second } : {}) };
}

function FrequencyStep(p: StepProps) {
  const [value, setValue] = useState<string>(frequencyOf(p.quiz));
  const submit = () => {
    if (!value) return p.setError("Choose one to continue.");
    void p.advance(async () => {
      const q = await api.quizAnswers({ pay_frequency: value });
      const s = scheduleFrom(q, { frequency: value as Frequency });
      return s && q.schedule ? api.quizSchedule(s) : q;
    });
  };
  return (
    <Shell title="How often are you paid?" sub="The plan follows your paychecks, not the calendar month." cta="Continue" busy={p.busy} error={p.error} onSubmit={submit}>
      <Options
        name="pay_frequency"
        legend="How often are you paid?"
        value={value}
        onChange={(v) => {
          setValue(v);
          p.setError(null);
        }}
        options={[
          { value: "weekly", label: "Every week", right: "52 paydays a year" },
          { value: "biweekly", label: "Every two weeks", right: "26 paydays a year" },
          { value: "semimonthly", label: "Twice a month", right: "24 paydays a year" },
          { value: "monthly", label: "Once a month", right: "12 paydays a year" },
        ]}
      />
    </Shell>
  );
}

function PaydayStep(p: StepProps) {
  const [date, setDate] = useState(paydayOf(p.quiz, p.today));
  const semi = frequencyOf(p.quiz) === "semimonthly";
  const [second, setSecond] = useState(String(Number(p.quiz.answers.second_payday_day) || p.quiz.schedule?.second_day || 15));
  const id = useId();
  const max = useMemo(() => {
    const d = new Date(`${p.today}T12:00:00`);
    d.setDate(d.getDate() + 35);
    return localISODate(d);
  }, [p.today]);
  const submit = () => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return p.setError("Choose the date of your next payday.");
    if (date < p.today) return p.setError("Choose today or a later date.");
    void p.advance(async () => {
      const q = await api.quizAnswers({ next_payday: date, ...(semi ? { second_payday_day: Number(second) } : {}) });
      const s = scheduleFrom(q, { anchor: date, ...(semi ? { second_day: Number(second) } : {}) });
      return s && q.schedule ? api.quizSchedule(s) : q;
    });
  };
  return (
    <Shell title="When is your next payday?" sub="The first instruction lands on this day." cta="Continue" busy={p.busy} error={p.error} onSubmit={submit}>
      <div className="form">
        <div className="fld">
          <label htmlFor={`${id}-date`}>Next payday</label>
          <input
            id={`${id}-date`}
            className="inp"
            type="date"
            value={date}
            min={p.today}
            max={max}
            onChange={(e) => {
              setDate(e.target.value);
              p.setError(null);
            }}
            data-testid="quiz-payday"
          />
        </div>
        {semi && (
          <div className="fld">
            <label htmlFor={`${id}-second`}>Your other payday each month</label>
            <select id={`${id}-second`} className="inp" value={second} onChange={(e) => setSecond(e.target.value)}>
              {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
                <option key={d} value={d}>
                  The {ordinal(d)}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>
    </Shell>
  );
}

function AmountStep(p: StepProps) {
  const [amount, setAmount] = useState(p.quiz.schedule ? centsToInput(p.quiz.schedule.per_payday_cents) : "");
  const id = useId();
  const submit = () => {
    const cents = parseMoneyToCents(amount);
    if (cents === null) return p.setError("Enter an amount, like 300.");
    const s = scheduleFrom(p.quiz, { per_payday_cents: cents });
    if (!s) return p.setError("Tell us how often you are paid and your next payday first.");
    void p.advance(() => api.quizSchedule(s));
  };
  return (
    <Shell
      title="How much from each paycheck can go to your cards?"
      sub="Everything for all cards together, minimums included."
      cta="Continue"
      busy={p.busy}
      error={p.error}
      onSubmit={submit}
    >
      <div className="form">
        <div className="fld">
          <label htmlFor={`${id}-amt`}>From each paycheck</label>
          <div className="money">
            <span>$</span>
            <input
              id={`${id}-amt`}
              className="inp num"
              inputMode="decimal"
              value={amount}
              onChange={(e) => {
                setAmount(e.target.value);
                p.setError(null);
              }}
              placeholder="0.00"
              autoComplete="off"
              aria-describedby={`${id}-hint`}
              data-testid="quiz-amount"
            />
          </div>
          <span className="hint" id={`${id}-hint`}>
            An amount you can repeat every payday. You can change it later, and a short payday never counts against you.
          </span>
        </div>
      </div>
    </Shell>
  );
}

// ---------- steps: circumstances ----------
function LateFeeStep(p: StepProps) {
  const a = p.quiz.answers;
  const [value, setValue] = useState(typeof a.late_fee_recently === "boolean" ? (a.late_fee_recently ? "yes" : "no") : "");
  const [card, setCard] = useState(str(a.late_fee_card) || p.quiz.cards[0]?.name || "");
  const [fee, setFee] = useState(typeof a.late_fee_cents === "number" ? centsToInput(a.late_fee_cents) : "");
  const id = useId();
  const submit = () => {
    if (!value) return p.setError("Choose one to continue.");
    if (value === "no") return void p.advance(() => api.quizAnswers({ late_fee_recently: false, late_fee_card: null, late_fee_cents: null }));
    const cents = parseMoneyToCents(fee);
    if (cents === null || cents === 0) return p.setError("Enter the fee as an amount, like 39. It is on the statement under Fees.");
    void p.advance(() => api.quizAnswers({ late_fee_recently: true, late_fee_card: card, late_fee_cents: cents }));
  };
  return (
    <Shell title="Any late fee in the last three months?" sub="It happens. A recent fee is often the first thing worth asking about." cta="Continue" busy={p.busy} error={p.error} onSubmit={submit}>
      <Options
        name="late_fee"
        legend="Any late fee in the last three months?"
        value={value}
        onChange={(v) => {
          setValue(v);
          p.setError(null);
        }}
        options={[
          { value: "yes", label: "Yes" },
          { value: "no", label: "No" },
        ]}
      />
      {value === "yes" && (
        <div className="form">
          <div className="fld">
            <label htmlFor={`${id}-card`}>Which card?</label>
            <select id={`${id}-card`} className="inp" value={card} onChange={(e) => setCard(e.target.value)} data-testid="quiz-late-card">
              {p.quiz.cards.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
          <div className="fld">
            <label htmlFor={`${id}-fee`}>How much was the fee?</label>
            <div className="money">
              <span>$</span>
              <input
                id={`${id}-fee`}
                className="inp num"
                inputMode="decimal"
                value={fee}
                onChange={(e) => {
                  setFee(e.target.value);
                  p.setError(null);
                }}
                placeholder="0.00"
                autoComplete="off"
                data-testid="quiz-late-fee"
              />
            </div>
          </div>
        </div>
      )}
    </Shell>
  );
}

// ---------- the quiz ----------
export default function Quiz() {
  const { step } = useParams();
  const navigate = useNavigate();
  const { me } = useSession();
  const today = me?.today ?? localISODate();
  const loaded = useLoad(() => api.quiz(), []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const quiz = loaded.data;

  useEffect(() => {
    quizStart(true);
  }, []);
  useEffect(() => {
    setError(null);
    if (step) api.track("quiz_step", { step });
  }, [step]);

  if (loaded.loading && !quiz) {
    return (
      <div className="app funnel">
        <FunnelTop backTo="/" />
        <Loading />
      </div>
    );
  }
  if (!quiz) {
    return (
      <div className="app funnel">
        <FunnelTop backTo="/" />
        <main className="body">
          <ErrorBox message={loaded.error ?? "We couldn't load the quiz."} onRetry={loaded.reload} />
        </main>
      </div>
    );
  }

  const steps = quizSteps(quiz);
  const open = firstOpenStep(quiz, today);
  if (!step) {
    if (!open && quiz.has_result) return <Navigate to="/result" replace />;
    return <Navigate to={`/quiz/${open ?? "autopay"}`} replace />;
  }
  const at = steps.indexOf(step);
  const openAt = open ? steps.indexOf(open) : steps.length - 1;
  // No jumping ahead of the first unanswered step.
  if (at === -1 || at > openAt) return <Navigate to={`/quiz/${open ?? "autopay"}`} replace />;

  const advance: StepProps["advance"] = async (save, then = "next") => {
    setBusy(true);
    setError(null);
    try {
      const saved = await save();
      const q = saved ?? quiz;
      if (saved) loaded.setData(saved);
      if (then === "result") {
        await api.makeResult();
        api.track("quiz_completed");
        navigate("/result");
        return;
      }
      const list = quizSteps(q);
      const next = list[list.indexOf(step) + 1];
      navigate(`/quiz/${next ?? "autopay"}`);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const back = () => navigate(at === 0 ? "/" : `/quiz/${steps[at - 1]}`);
  const p: StepProps = { quiz, today, advance, busy, error, setError, setQuiz: loaded.setData };

  let view: ReactNode;
  if (step === "count") view = <CountStep {...p} />;
  else if (step === "age")
    view = (
      <ChoiceStep
        p={p}
        answerKey="age_band"
        title="How old are you?"
        sub="Burn Rate is for adults in the United States."
        options={[
          { value: "18-24", label: "18 to 24" },
          { value: "25-34", label: "25 to 34" },
          { value: "35-44", label: "35 to 44" },
          { value: "45-54", label: "45 to 54" },
          { value: "55+", label: "55 or older" },
        ]}
      />
    );
  else if (step === "carry")
    view = (
      <ChoiceStep
        p={p}
        answerKey="carry_months"
        title="How long have you carried a balance?"
        sub="Roughly is fine."
        options={[
          { value: "under_3", label: "Less than 3 months" },
          { value: "3_to_12", label: "3 to 12 months" },
          { value: "over_12", label: "More than a year" },
          { value: "on_and_off", label: "On and off" },
        ]}
      />
    );
  else if (step.startsWith("card-")) view = <CardStep p={p} index={Number(step.slice(5)) - 1} />;
  else if (step === "frequency") view = <FrequencyStep {...p} />;
  else if (step === "payday") view = <PaydayStep {...p} />;
  else if (step === "amount") view = <AmountStep {...p} />;
  else if (step === "late-fee") view = <LateFeeStep {...p} />;
  else view = <AutopayStep p={p} />;

  return (
    <div className="app funnel">
      <FunnelTop step={at + 1} of={steps.length} onBack={back} />
      {/* The key resets each step's local state when the route changes. */}
      <div key={step} style={{ display: "contents" }}>
        {view}
      </div>
    </div>
  );
}

function AutopayStep({ p }: { p: StepProps }) {
  const [value, setValue] = useState(str(p.quiz.answers.autopay));
  const submit = () => {
    if (!value) return p.setError("Choose one to continue.");
    void p.advance(() => api.quizAnswers({ autopay: value }), "result");
  };
  return (
    <Shell title="How is autopay set on your cards?" sub="Autopay on the minimum is the safety net behind the plan." cta="See my number" busy={p.busy} error={p.error} onSubmit={submit}>
      <Options
        name="autopay"
        legend="How is autopay set on your cards?"
        value={value}
        onChange={(v) => {
          setValue(v);
          p.setError(null);
        }}
        options={[
          { value: "min", label: "Minimum payment" },
          { value: "full", label: "Full balance" },
          { value: "none", label: "It's off" },
          { value: "unsure", label: "Not sure" },
        ]}
      />
    </Shell>
  );
}
