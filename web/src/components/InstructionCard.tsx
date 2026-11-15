import { useId, useState } from "react";
import type { FormEvent } from "react";
import type { Instruction, Send } from "../api/types";
import { dateLong, dateShort, hostLabel, parseMoneyToCents, pct, usd } from "../lib/format";
import { Plastic } from "./Bits";
import { OutIcon } from "./Icons";

export interface InstructionCardProps {
  instruction: Instruction;
  /** The user's current date from the API (demo clock applied). Decides between "Send today" and "Ready to send". */
  today?: string;
  /** Card ids that carry the user's highest rate, so the line can say "your highest" only when it is true. */
  highestRateCardIds?: string[];
  busy?: boolean;
  error?: string | null;
  onPaid: (amountCents?: number) => void;
  onUndo: () => void;
  onShort: (availableCents: number) => void;
}

/** The sentence under the amount, built from the instruction's own fields. */
export function explain(send: Send, status: Instruction["status"], highest: boolean): { main: string[]; saved: string | null } {
  const due = dateShort(send.due);
  const main: string[] = [];
  if (send.covers_min) {
    main.push(`Covers its ${usd(send.min_cents)} minimum due ${due}`);
    if (send.extra_cents > 0) main.push(` and puts **${usd(send.extra_cents)} extra** on ${highest ? "your highest rate" : "this card"}`);
    main.push(".");
  } else {
    main.push(`This is less than its ${usd(send.min_cents)} minimum due ${due}. Keep autopay on so the minimum is paid on time.`);
  }
  let saved: string | null = null;
  if (send.saved_by_date_cents > 0) {
    const when = status === "due" || status === "short" ? "today" : "on payday";
    saved = ` Paying ${when} instead of on the due date saves about **${usd(send.saved_by_date_cents)}** in interest.`;
  }
  return { main, saved };
}

function Rich({ text }: { text: string }) {
  const parts = text.split("**");
  return (
    <>
      {parts.map((part, i) => (i % 2 ? <b key={i}>{part}</b> : part))}
    </>
  );
}

export default function InstructionCard({ instruction: ins, today, highestRateCardIds = [], busy = false, error, onPaid, onUndo, onShort }: InstructionCardProps) {
  const [panel, setPanel] = useState<"none" | "short" | "amount">("none");
  const [value, setValue] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const id = useId();
  const paid = ins.status === "paid";
  const upcoming = ins.status === "upcoming";
  const actionable = ins.status === "due" || ins.status === "short";
  const onItsDay = !today || ins.payday === today;
  const label = paid ? (onItsDay ? "Paid today" : "Paid") : upcoming ? "Next payday" : onItsDay ? "Send today" : "Ready to send";
  const first = ins.sends[0];

  const submitPanel = (e: FormEvent) => {
    e.preventDefault();
    const cents = parseMoneyToCents(value);
    if (cents === null) return setFormError("Enter an amount, like 150.");
    setFormError(null);
    if (panel === "short") onShort(cents);
    else {
      if (cents === 0) return setFormError("Enter the amount you paid.");
      onPaid(cents);
    }
    setPanel("none");
    setValue("");
  };

  const toggle = (next: "short" | "amount") => {
    setPanel((p) => (p === next ? "none" : next));
    setValue("");
    setFormError(null);
  };

  return (
    <section className={`send blk-send ${paid ? "done" : ""}`} aria-label="Payday instruction">
      <div className="lab">
        <span data-testid="today-label">{label}</span>
        <span>{dateLong(ins.payday)}</span>
      </div>
      {ins.sends.map((send, i) => {
        const highest = highestRateCardIds.includes(send.card_id);
        const amount = paid && ins.paid_cents !== null && ins.sends.length === 1 ? ins.paid_cents : send.amount_cents;
        const text = explain(send, ins.status, highest);
        return (
          <div key={send.card_id} className={i ? "more" : undefined}>
            <div className="amt num" data-testid={i ? undefined : "today-amount"}>
              {usd(amount)}
            </div>
            <div className="to">
              <Plastic issuer={send.issuer} />
              <div>
                <b className="nm" data-testid={i ? undefined : "today-card"}>
                  {send.card_name}
                </b>
                <span className="num">
                  {send.last4 ? `•• ${send.last4} · ` : ""}
                  {pct(send.apr_bps)} APR{highest ? ", your highest" : ""}
                </span>
              </div>
            </div>
            <p className="why num">
              <Rich text={text.main.join("")} />
              {text.saved && <Rich text={text.saved} />}
              {!highest && ins.reason ? ` ${ins.reason}` : ""}
            </p>
          </div>
        );
      })}
      {ins.status === "short" && (
        <p className="why num" data-testid="today-short-note">
          Adjusted for this payday{ins.short_by_cents > 0 ? <>: {usd(ins.short_by_cents)} less than the usual plan</> : null}. Nothing is marked as missed, and your plan picks up
          again next payday.
        </p>
      )}

      {paid ? (
        <div className="btns">
          <button className="ghost" type="button" onClick={onUndo} disabled={busy} data-testid="today-undo">
            Undo
          </button>
        </div>
      ) : (
        <div className={`btns ${first?.pay_url && actionable ? "two-up" : ""}`}>
          {first?.pay_url && (
            <a className="ghost" href={first.pay_url} target="_blank" rel="noopener noreferrer" data-testid="today-pay-link">
              Pay at {hostLabel(first.pay_url)} <OutIcon />
              <span className="sr">(opens in a new tab)</span>
            </a>
          )}
          {actionable && (
            <button className="cta" type="button" onClick={() => onPaid()} disabled={busy} data-testid="today-paid">
              Mark as paid
            </button>
          )}
        </div>
      )}

      {actionable && (
        <div className="quiet">
          <button className="textlink quiet" type="button" onClick={() => toggle("short")} aria-expanded={panel === "short"} data-testid="today-short">
            Short this payday?
          </button>
          <button className="textlink quiet" type="button" onClick={() => toggle("amount")} aria-expanded={panel === "amount"} data-testid="today-other-amount">
            Paid a different amount?
          </button>
        </div>
      )}
      {actionable && panel !== "none" && (
        <form className="inline" onSubmit={submitPanel} noValidate>
          <label htmlFor={`${id}-v`}>{panel === "short" ? "How much can go to your cards this payday?" : "How much did you pay?"}</label>
          <div className="go">
            <div className="money">
              <span>$</span>
              <input id={`${id}-v`} className="inp num" inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} placeholder="0.00" autoComplete="off" autoFocus data-testid="today-inline-amount" />
            </div>
            <button className="btn field-h" type="submit" disabled={busy} data-testid="today-inline-save">
              {panel === "short" ? "Recalculate" : "Mark as paid"}
            </button>
          </div>
          {panel === "short" && <p className="fine" style={{ margin: 0 }}>Minimums come first. A smaller payday is not a missed one.</p>}
          {formError && (
            <p className="err" role="alert" style={{ margin: 0 }}>
              {formError}
            </p>
          )}
        </form>
      )}
      {error && (
        <p className="err" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
