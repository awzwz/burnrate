import { useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../../api/client";
import type { CardView, Refinance, TransferOption } from "../../api/types";
import { ErrorBox, Loading, Plastic } from "../../components/Bits";
import { Chevron } from "../../components/Icons";
import { useShell } from "../../components/ProductShell";
import { dateShort, monthsText, ordinal, parseMoneyToCents, pct, usd, usdPerDay } from "../../lib/format";
import { useLoad } from "../../lib/useLoad";

const AUTOPAY: Record<string, string> = { min: "Minimum payment", full: "Full balance", none: "Off" };
const LEDGER: Record<string, string> = {
  opening: "Starting balance",
  purchase: "Purchase",
  fee: "Fee",
  interest: "Interest",
  payment: "Payment",
  credit: "Credit",
  adjustment: "Adjustment",
  rate_change: "Rate change",
};

function EditCard({ card, onSaved }: { card: CardView; onSaved: (c: CardView) => void }) {
  const [due, setDue] = useState(String(card.due_day));
  const [close, setClose] = useState(String(card.close_day));
  const [autopay, setAutopay] = useState(card.autopay);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const id = useId();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      onSaved(await api.patchCard(card.id, { due_day: Number(due), close_day: Number(close), autopay }));
      setMsg({ ok: true, text: "Saved. Your plan uses the new settings." });
    } catch (err) {
      setMsg({ ok: false, text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="tile" onSubmit={submit} noValidate aria-label="Card settings">
      <h3>Dates and autopay</h3>
      <div className="form" style={{ marginTop: 12 }}>
        <div className="two">
          <div className="fld">
            <label htmlFor={`${id}-due`}>Payment due day</label>
            <select id={`${id}-due`} className="inp" value={due} onChange={(e) => setDue(e.target.value)} data-testid="card-edit-due">
              {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
                <option key={d} value={d}>
                  {ordinal(d)}
                </option>
              ))}
            </select>
          </div>
          <div className="fld">
            <label htmlFor={`${id}-close`}>Statement closing day</label>
            <select id={`${id}-close`} className="inp" value={close} onChange={(e) => setClose(e.target.value)} data-testid="card-edit-close">
              {Array.from({ length: 31 }, (_, i) => i + 1).map((d) => (
                <option key={d} value={d}>
                  {ordinal(d)}
                </option>
              ))}
            </select>
          </div>
        </div>
        <div className="fld">
          <label htmlFor={`${id}-auto`}>Autopay at your bank</label>
          <select id={`${id}-auto`} className="inp" value={autopay} onChange={(e) => setAutopay(e.target.value)} aria-describedby={`${id}-auto-hint`} data-testid="card-edit-autopay">
            {Object.entries(AUTOPAY).map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
          <span className="hint" id={`${id}-auto-hint`}>
            Set this to match your bank. Burn Rate cannot change autopay for you; minimum autopay is the safety net behind the plan.
          </span>
        </div>
      </div>
      {msg && (
        <p className={msg.ok ? "err okline" : "err"} role={msg.ok ? "status" : "alert"}>
          {msg.text}
        </p>
      )}
      <div className="row-btns" style={{ marginTop: 16 }}>
        <button className="btn" type="submit" disabled={busy} data-testid="card-edit-save">
          {busy ? "Saving" : "Save"}
        </button>
      </div>
    </form>
  );
}

function NewCharge({ card, onSaved }: { card: CardView; onSaved: (c: CardView) => void }) {
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const id = useId();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const cents = parseMoneyToCents(amount);
    if (cents === null || cents === 0) return setMsg({ ok: false, text: "Enter the charge as an amount, like 84.20." });
    setBusy(true);
    setMsg(null);
    try {
      const next = await api.addCharge(card.id, { amount_cents: cents, ...(note.trim() ? { note: note.trim() } : {}) });
      onSaved(next);
      setAmount("");
      setNote("");
      setMsg({ ok: true, text: `Added. This card now costs ${usdPerDay(next.burn_per_day)} a day and the plan is recalculated.` });
    } catch (err) {
      setMsg({ ok: false, text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="tile" onSubmit={submit} noValidate aria-label="A new charge landed">
      <h3>A new charge landed</h3>
      <p>It happens. Add it and the plan recalculates around it.</p>
      <div className="form" style={{ marginTop: 12 }}>
        <div className="two">
          <div className="fld">
            <label htmlFor={`${id}-amt`}>Amount</label>
            <div className="money">
              <span>$</span>
              <input id={`${id}-amt`} className="inp num" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="0.00" autoComplete="off" data-testid="charge-amount" />
            </div>
          </div>
          <div className="fld">
            <label htmlFor={`${id}-note`}>Note (optional)</label>
            <input id={`${id}-note`} className="inp" value={note} onChange={(e) => setNote(e.target.value)} maxLength={80} autoComplete="off" />
          </div>
        </div>
      </div>
      {msg && (
        <p className={msg.ok ? "err okline" : "err"} role={msg.ok ? "status" : "alert"}>
          {msg.text}
        </p>
      )}
      <div className="row-btns" style={{ marginTop: 16 }}>
        <button className="ghost" type="submit" disabled={busy} data-testid="charge-save">
          {busy ? "Adding" : "Add charge"}
        </button>
      </div>
    </form>
  );
}

function saving(cents: number): string {
  if (cents > 0) return `${usd(cents)} less than changing nothing`;
  if (cents < 0) return `${usd(-cents)} more than changing nothing`;
  return "the same as changing nothing";
}

function TransferRow({ label, t, promo }: { label: string; t: TransferOption; promo: number }) {
  return (
    <div className="d num">
      <b>{label}:</b> costs {usd(t.transfer_cost_cents)} in fee and interest, {saving(t.saving_cents)}. Paid off in {monthsText(t.transfer_months)}.{" "}
      {t.clears_in_promo ? `Clears inside the ${promo} months.` : `To clear it inside the ${promo} months you would need ${usd(t.payment_to_clear_in_promo_cents)} a month.`}
      {t.breakeven_months !== null && ` The fee pays for itself after ${monthsText(t.breakeven_months)}.`}
    </div>
  );
}

function RefinanceBlock({ card, version }: { card: CardView; version: number }) {
  const [payment, setPayment] = useState("");
  const [applied, setApplied] = useState<number | undefined>(undefined);
  const [data, setData] = useState<Refinance | null>(null);
  const [error, setError] = useState<string | null>(null);
  const id = useId();

  useEffect(() => {
    const ctl = new AbortController();
    const t = window.setTimeout(
      () => {
        api
          .refinance(card.id, applied, ctl.signal)
          .then((r) => {
            setData(r);
            setError(null);
          })
          .catch((e) => {
            if (!ctl.signal.aborted) setError(errorMessage(e));
          });
      },
      applied === undefined ? 0 : 350,
    );
    return () => {
      ctl.abort();
      window.clearTimeout(t);
    };
  }, [card.id, applied, version]);

  const onChange = (v: string) => {
    setPayment(v);
    const cents = parseMoneyToCents(v);
    if (v.trim() === "") setApplied(undefined);
    else if (cents !== null && cents > 0) setApplied(cents);
  };

  return (
    <section aria-label="Refinance comparison">
      <div className="sec">
        <h2>Would moving this balance cost less?</h2>
      </div>
      <div className="fld" style={{ maxWidth: 320 }}>
        <label htmlFor={`${id}-pay`}>Monthly payment to compare</label>
        <div className="money">
          <span>$</span>
          <input id={`${id}-pay`} className="inp num" inputMode="decimal" value={payment} onChange={(e) => onChange(e.target.value)} placeholder="your plan's amount" autoComplete="off" aria-describedby={`${id}-pay-hint`} data-testid="refinance-payment" />
        </div>
        <span className="hint" id={`${id}-pay-hint`}>
          Leave empty to use what your plan sends to this card.
        </span>
      </div>
      {error && <ErrorBox message={error} />}
      {data && (
        <>
          <div className="opt3 three" style={{ marginTop: 16 }} data-testid="refinance">
            <div>
              <div className="k">Change nothing</div>
              <div className="v num">{usd(data.stay.interest_cents)}</div>
              <div className="d num">
                in interest, paid off in {monthsText(data.stay.months)} at {pct(card.apr_bps)}.
              </div>
            </div>
            <div>
              <div className="k">Balance transfer, 0% for {data.transfer.promo_months} months</div>
              <div className="v num">{usd(data.transfer.fee_3.transfer_cost_cents)}</div>
              <TransferRow label="With a 3% fee" t={data.transfer.fee_3} promo={data.transfer.promo_months} />
              <TransferRow label="With a 5% fee" t={data.transfer.fee_5} promo={data.transfer.promo_months} />
            </div>
            <div>
              <div className="k">Personal loan</div>
              <div className="v num">{data.loans[0] ? usd(data.loans[0].loan_cost_cents) : "—"}</div>
              {data.loans.map((l) => (
                <div className="d num" key={`${l.loan_apr_bps}-${l.origination_bps}`}>
                  <b>
                    At {pct(l.loan_apr_bps)} with a {pct(l.origination_bps)} fee:
                  </b>{" "}
                  {usd(l.payment_cents)} a month, costs {usd(l.loan_cost_cents)} in fee and interest, {saving(l.saving_cents)}.
                </div>
              ))}
            </div>
          </div>
          <p className="fine">{data.note} Burn Rate does not offer or broker loans or cards and earns nothing from either choice.</p>
        </>
      )}
      {!data && !error && <Loading />}
    </section>
  );
}

export default function CardDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const shell = useShell();
  const cards = useLoad(() => api.cards(), []);
  const ledger = useLoad(() => api.ledger(id), [id]);
  const [version, setVersion] = useState(0);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  if (!cards.data) return cards.loading ? <Loading /> : <ErrorBox message={cards.error ?? "We couldn't load this card."} onRetry={cards.reload} />;
  const card = cards.data.cards.find((c) => c.id === id);
  if (!card) {
    return (
      <>
        <Link className="crumb" to="/app/cards">
          <Chevron /> Cards
        </Link>
        <ErrorBox message="We couldn't find that card." />
      </>
    );
  }

  const saved = (next: CardView) => {
    cards.setData({ ...cards.data!, cards: cards.data!.cards.map((c) => (c.id === next.id ? next : c)) });
    cards.reload();
    ledger.reload();
    shell.refresh();
    setVersion((v) => v + 1);
  };

  const remove = async () => {
    setRemoveError(null);
    try {
      await api.deleteCard(card.id);
      shell.refresh();
      navigate("/app/cards");
    } catch (e) {
      setRemoveError(errorMessage(e));
    }
  };

  return (
    <>
      <Link className="crumb" to="/app/cards">
        <Chevron /> Cards
      </Link>
      <div className="pd-h">
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <Plastic issuer={card.issuer} />
          <div>
            <h1>{card.name}</h1>
            <p className="lead num">
              {card.last4 ? `•• ${card.last4} · ` : ""}
              {pct(card.apr_bps)} APR
            </p>
          </div>
        </div>
        <span className="chip num">
          <i />
          {usdPerDay(card.burn_per_day)} / day
        </span>
      </div>

      <div className="cgrid" style={{ marginTop: 18 }}>
        <div>
          <dl className="kv num">
            <dt>Balance</dt>
            <dd>
              {usd(card.balance_cents)}
              <small>
                started at {usd(card.start_balance_cents)}, {Math.round(card.progress * 100)}% paid down
              </small>
            </dd>
            <dt>Costs you</dt>
            <dd>{usdPerDay(card.burn_per_day)} a day</dd>
            <dt>Minimum payment</dt>
            <dd>
              {usd(card.min_payment_cents)}
              <small>{card.min_rule_verified ? `due ${dateShort(card.next_due)}` : `due ${dateShort(card.next_due)} · an estimate until a statement confirms it`}</small>
            </dd>
            <dt>Statement closes</dt>
            <dd>the {ordinal(card.close_day)}</dd>
            <dt>Autopay</dt>
            <dd>{AUTOPAY[card.autopay] ?? card.autopay}</dd>
          </dl>
          {card.pay_url && (
            <p style={{ marginTop: 12 }}>
              <a className="textlink" href={card.pay_url} target="_blank" rel="noopener noreferrer">
                Open {new URL(card.pay_url).hostname.replace(/^www\./, "")}
                <span className="sr"> (opens in a new tab)</span>
              </a>
            </p>
          )}

          <div style={{ marginTop: 20 }}>
            <NewCharge card={card} onSaved={saved} />
            <EditCard card={card} onSaved={saved} key={`${card.due_day}-${card.close_day}-${card.autopay}`} />
          </div>
        </div>

        <div>
          <div className="sec" style={{ marginTop: 0 }}>
            <h2>Ledger</h2>
            <span>newest first</span>
          </div>
          {ledger.data ? (
            <ul className="list num" data-testid="card-ledger">
              {ledger.data.events.map((e) => (
                <li key={e.id}>
                  <div className="nm">
                    {LEDGER[e.type] ?? e.type}
                    <small>
                      {dateShort(e.date)}
                      {e.note ? ` · ${e.note}` : ""}
                    </small>
                  </div>
                  <div className="v">{e.type === "rate_change" ? "" : usd(e.signed_cents)}</div>
                </li>
              ))}
              {ledger.data.events.length === 0 && (
                <li>
                  <div className="nm">
                    <small>Nothing recorded yet.</small>
                  </div>
                </li>
              )}
            </ul>
          ) : ledger.loading ? (
            <Loading />
          ) : (
            <ErrorBox message={ledger.error ?? "We couldn't load the ledger."} onRetry={ledger.reload} />
          )}
        </div>
      </div>

      {!card.paid_off && <RefinanceBlock card={card} version={version} />}

      <div style={{ marginTop: 32 }}>
        {removing ? (
          <div className="note" role="group" aria-label="Remove this card">
            <p>
              <b>Remove {card.name}?</b> Its balance leaves your daily number and the plan stops sending money to it.
            </p>
            <div className="row-btns" style={{ marginTop: 12 }}>
              <button className="ghost plain sm" type="button" onClick={remove} data-testid="card-remove-confirm">
                Remove card
              </button>
              <button className="ghost sm" type="button" onClick={() => setRemoving(false)}>
                Keep it
              </button>
            </div>
            {removeError && <p className="err">{removeError}</p>}
          </div>
        ) : (
          <button className="textlink quiet" type="button" onClick={() => setRemoving(true)} data-testid="card-remove">
            Remove this card
          </button>
        )}
      </div>
    </>
  );
}
