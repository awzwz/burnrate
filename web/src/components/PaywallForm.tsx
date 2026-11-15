import { useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { Plan } from "../api/types";
import { periodText, usd, usdPerDay } from "../lib/format";
import { Shield } from "./Icons";

export interface PaywallFormProps {
  plans: Plan[];
  guaranteeDays: number;
  /** Cents per day from the stored quiz result, when there is one. */
  burnPerDay: number | null;
  defaultEmail?: string;
  testMode: boolean;
  /** Called only when the renewal box is ticked and the email looks like one. */
  onStart: (planId: string, email: string) => Promise<void>;
  error?: string | null;
}

/** "after the first 12 weeks for $39.99 the plan renews automatically at $79.99 every 12 weeks" */
export function renewalSentence(p: Plan): { first: string; price: string; renewal: string } {
  const first = periodText(p.days);
  return {
    first: first === "week" || first === "day" ? first : first,
    price: usd(p.price_cents),
    renewal: `${usd(p.renews_price_cents)} every ${periodText(p.renews_days)}`,
  };
}

export default function PaywallForm({ plans, guaranteeDays, burnPerDay, defaultEmail = "", testMode, onStart, error }: PaywallFormProps) {
  const preselected = plans.find((p) => p.tag) ?? plans[plans.length - 1];
  const [chosen, setChosen] = useState(preselected?.id ?? "");
  const [agree, setAgree] = useState(false);
  const [needConsent, setNeedConsent] = useState(false);
  const [email, setEmail] = useState(defaultEmail);
  const [emailError, setEmailError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const consentRef = useRef<HTMLLabelElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const id = useId();
  const plan = plans.find((p) => p.id === chosen) ?? preselected;
  if (!plan) return null;
  const terms = renewalSentence(plan);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const mail = email.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(mail)) {
      setEmailError("Enter your email address. The receipt and renewal reminders go there.");
      emailRef.current?.focus();
      return;
    }
    if (!agree) {
      setNeedConsent(true);
      const el = consentRef.current;
      if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "center" });
      el?.querySelector("input")?.focus();
      return;
    }
    setBusy(true);
    try {
      await onStart(plan.id, mail);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} noValidate style={{ display: "contents" }}>
      <main className="body" style={{ paddingTop: 16 }}>
        <h1 className="title">Your plan is ready</h1>
        <p className="sub num">
          {burnPerDay !== null && <>Your cards burn {usdPerDay(burnPerDay)} a day. </>}
          This plan costs <span data-testid="plan-per-day">{usdPerDay(plan.per_day_cents)}</span> a day.
        </p>

        <ul className="incl">
          <li>
            <CheckMark />
            <span>
              <b>A payday instruction</b> for every paycheck: one amount, one card, one day.
            </span>
          </li>
          <li>
            <CheckMark />
            <span>
              <b>A statement check</b> every month: our forecast next to your bank's number.
            </span>
          </li>
          <li>
            <CheckMark />
            <span>
              <b>A 14-day ask sprint:</b> what to ask each issuer, rehearsal, a call card. You make each call yourself; an issuer may say no.
            </span>
          </li>
        </ul>

        <fieldset className="plans">
          <legend className="sr">Plan</legend>
          {plans.map((p) => (
            <label className="plan" key={p.id} data-testid={`plan-option-${p.id}`}>
              <input
                type="radio"
                name="plan"
                value={p.id}
                checked={p.id === plan.id}
                onChange={() => {
                  setChosen(p.id);
                  // New plan, new renewal price: the consent has to be given again for it.
                  setAgree(false);
                }}
              />
              {p.tag && <span className="tag">{p.tag}</span>}
              <span className="nm">{p.name}</span>
              <span className="price num">
                <b>{usd(p.price_cents)}</b>
                <span>{usdPerDay(p.per_day_cents)} a day</span>
              </span>
              <span className="then num">{p.then}</span>
            </label>
          ))}
        </fieldset>

        <div className="form">
          <div className="fld">
            <label htmlFor={`${id}-email`}>Email</label>
            <input
              id={`${id}-email`}
              ref={emailRef}
              className="inp"
              type="email"
              inputMode="email"
              autoComplete="email"
              value={email}
              onChange={(e) => {
                setEmail(e.target.value);
                setEmailError(null);
              }}
              placeholder="you@example.com"
              aria-invalid={!!emailError}
              aria-describedby={emailError ? `${id}-email-err` : `${id}-email-hint`}
              data-testid="plan-email"
            />
            {emailError ? (
              <span className="err" id={`${id}-email-err`} role="alert" style={{ margin: 0 }}>
                {emailError}
              </span>
            ) : (
              <span className="hint" id={`${id}-email-hint`}>
                For your receipt, sign-in link and a reminder before every renewal.
              </span>
            )}
          </div>
        </div>

        <label className={`consent ${needConsent ? "need" : ""}`} ref={consentRef}>
          <input
            type="checkbox"
            checked={agree}
            onChange={(e) => {
              setAgree(e.target.checked);
              if (e.target.checked) setNeedConsent(false);
            }}
            aria-invalid={needConsent}
            aria-describedby={needConsent ? `${id}-consent-err` : undefined}
            data-testid="plan-consent"
          />
          <span className="num" data-testid="plan-consent-text">
            I understand that after the first {terms.first} for <b>{terms.price}</b> the plan renews automatically at <b>{terms.renewal}</b> until I
            cancel. Cancelling takes two steps in Settings.
          </span>
        </label>
        {needConsent && (
          <p className="err" id={`${id}-consent-err`} role="alert">
            Please tick the box to confirm the renewal terms.
          </p>
        )}
        {error && (
          <p className="err" role="alert">
            {error}
          </p>
        )}
      </main>
      <div className="cta-wrap">
        <button className="cta" type="submit" disabled={busy} data-testid="plan-cta">
          {busy ? "One moment" : `Start for ${usd(plan.price_cents)}`}
        </button>
        <p className="guar">
          <Shield /> {guaranteeDays}-day money-back. No conditions.
        </p>
        <p className="pay-note">{testMode ? "Card details on the next step. Test mode: no real charge." : "Card details on the next step."}</p>
      </div>
    </form>
  );
}

function CheckMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 9.5l3.5 3.5 7.5-8" />
    </svg>
  );
}
