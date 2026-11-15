import { useId, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../../api/client";
import { ErrorBox, Loading, Plastic } from "../../components/Bits";
import { useShell } from "../../components/ProductShell";
import { dateShort, ordinal, parseAprToBps, parseMoneyToCents, pct, usd, usdPerDay } from "../../lib/format";
import { useLoad } from "../../lib/useLoad";

function AddCard({ onAdded, onCancel }: { onAdded: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [balance, setBalance] = useState("");
  const [apr, setApr] = useState("");
  const [min, setMin] = useState("");
  const [due, setDue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const balance_cents = parseMoneyToCents(balance);
    const apr_bps = parseAprToBps(apr);
    if (!name.trim()) return setError("Give the card a name.");
    if (balance_cents === null) return setError("Enter the balance as an amount, like 1,250.00.");
    if (apr_bps === null) return setError("Enter the interest rate as a number, like 24.99.");
    let statement_min_cents: number | null = null;
    if (min.trim()) {
      statement_min_cents = parseMoneyToCents(min);
      if (statement_min_cents === null) return setError("Enter the minimum payment as an amount, or leave it empty.");
    }
    if (!due) return setError("Choose the day of the month the payment is due.");
    setBusy(true);
    setError(null);
    try {
      await api.addCard({ name: name.trim(), balance_cents, apr_bps, statement_min_cents, due_day: Number(due) });
      onAdded();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <form className="tile" onSubmit={submit} noValidate style={{ marginTop: 16 }} aria-label="Add a card">
      <h3>Add a card</h3>
      <p>Four numbers from the latest statement. No card number, no bank login.</p>
      <div className="form" style={{ marginTop: 12 }}>
        <div className="fld">
          <label htmlFor={`${id}-name`}>Card name</label>
          <input id={`${id}-name`} className="inp" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} autoComplete="off" data-testid="card-name" />
        </div>
        <div className="two">
          <div className="fld">
            <label htmlFor={`${id}-bal`}>Balance</label>
            <div className="money">
              <span>$</span>
              <input id={`${id}-bal`} className="inp num" inputMode="decimal" value={balance} onChange={(e) => setBalance(e.target.value)} placeholder="0.00" autoComplete="off" data-testid="card-balance" />
            </div>
          </div>
          <div className="fld">
            <label htmlFor={`${id}-apr`}>APR</label>
            <div className="money pct">
              <input id={`${id}-apr`} className="inp num" inputMode="decimal" value={apr} onChange={(e) => setApr(e.target.value)} placeholder="0.00" autoComplete="off" data-testid="card-apr" />
              <span>%</span>
            </div>
          </div>
        </div>
        <div className="two">
          <div className="fld">
            <label htmlFor={`${id}-min`}>Minimum payment</label>
            <div className="money">
              <span>$</span>
              <input id={`${id}-min`} className="inp num" inputMode="decimal" value={min} onChange={(e) => setMin(e.target.value)} placeholder="0.00" autoComplete="off" data-testid="card-min" />
            </div>
          </div>
          <div className="fld">
            <label htmlFor={`${id}-due`}>Due day</label>
            <select id={`${id}-due`} className="inp" value={due} onChange={(e) => setDue(e.target.value)} data-testid="card-due">
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
      {error && (
        <p className="err" role="alert">
          {error}
        </p>
      )}
      <div className="row-btns" style={{ marginTop: 16 }}>
        <button className="btn" type="submit" disabled={busy} data-testid="card-save">
          {busy ? "Saving" : "Add card"}
        </button>
        <button className="ghost plain" type="button" onClick={onCancel}>
          Not now
        </button>
      </div>
    </form>
  );
}

export default function Cards() {
  const { data, error, loading, reload } = useLoad(() => api.cards(), []);
  const shell = useShell();
  const [adding, setAdding] = useState(false);

  if (!data) return loading ? <Loading /> : <ErrorBox message={error ?? "We couldn't load your cards."} onRetry={reload} />;

  return (
    <div className="narrow">
      <div className="pd-h">
        <div>
          <h1>Cards</h1>
          <p className="lead">What each card costs you per day, and how far it has come down.</p>
        </div>
        <span className="chip num">
          <i />
          {usdPerDay(data.burn_per_day)} / day
        </span>
      </div>

      <div style={{ marginTop: 18 }}>
        {data.cards.map((c) => (
          <Link className="ccard num" to={`/app/cards/${c.id}`} key={c.id} data-testid="card-item">
            <div className="hd">
              <Plastic issuer={c.issuer} />
              <div className="nm">
                {c.name}
                <span>
                  {c.last4 ? `•• ${c.last4} · ` : ""}
                  {pct(c.apr_bps)} APR
                </span>
              </div>
              <div className="bal">
                {usd(c.balance_cents)}
                <span>{c.paid_off ? "paid off" : `${usdPerDay(c.burn_per_day)} a day`}</span>
              </div>
            </div>
            <div className={`bar ${c.paid_off ? "ok" : ""}`} role="img" aria-label={`${Math.round(c.progress * 100)}% paid down since you started`}>
              <i style={{ width: `${Math.round(c.progress * 100)}%` }} />
            </div>
            <div className="ft">
              <span>{Math.round(c.progress * 100)}% paid down since you started</span>
              {!c.paid_off && (
                <span>
                  {usd(c.min_payment_cents)} due {dateShort(c.next_due)}
                </span>
              )}
            </div>
          </Link>
        ))}
        {data.cards.length === 0 && <p className="note">No cards yet. Add one to get a payday plan.</p>}
      </div>

      <div className="total num">
        <span>Total balance</span>
        <span>
          <b>{usd(data.total_balance_cents)}</b>
          <span>{usdPerDay(data.burn_per_day)} in interest a day</span>
        </span>
      </div>

      {adding ? (
        <AddCard
          onCancel={() => setAdding(false)}
          onAdded={() => {
            setAdding(false);
            reload();
            shell.refresh();
          }}
        />
      ) : (
        <div className="row-btns" style={{ marginTop: 16 }}>
          <button className="ghost" type="button" onClick={() => setAdding(true)} data-testid="card-add">
            Add a card
          </button>
        </div>
      )}
    </div>
  );
}
