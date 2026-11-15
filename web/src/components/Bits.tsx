import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { splitPerDay } from "../lib/format";
import { Mark } from "./Icons";

export function Logo({ size = 20, to = "/" }: { size?: number; to?: string }) {
  return (
    <Link className="logo" to={to} aria-label="Burn Rate, home">
      <i className="mark">
        <Mark size={size} />
      </i>
      <span>Burn Rate</span>
    </Link>
  );
}

/** A small card-shaped swatch; colour follows the issuer the server reports. */
export function Plastic({ issuer }: { issuer?: string }) {
  return <i className={`plastic ${issuer ?? ""}`} aria-hidden="true" />;
}

/** The big "$7.38 in interest every day" counter. `value` is cents per day from the API. */
export function BigBurn({ value, testId }: { value: number; testId?: string }) {
  const { whole, cents } = splitPerDay(value);
  return (
    <div className="burn num">
      <span className="big" data-testid={testId} aria-label={`$${whole}.${cents} in interest every day`}>
        <small>$</small>
        {whole}.{cents}
      </span>
      <span className="unit" aria-hidden="true">
        in interest
        <br />
        every day
      </span>
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <p className="loading" role="status">
      <i className="dot" />
      {label}
    </p>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="note" role="alert" style={{ marginTop: 16 }}>
      <p>{message}</p>
      {onRetry && (
        <p style={{ marginTop: 8 }}>
          <button className="textlink" type="button" onClick={onRetry}>
            Try again
          </button>
        </p>
      )}
    </div>
  );
}

export function Field({ label, htmlFor, hint, error, children }: { label: string; htmlFor: string; hint?: ReactNode; error?: string | null; children: ReactNode }) {
  return (
    <div className="fld">
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && !error && <span className="hint" id={`${htmlFor}-hint`}>{hint}</span>}
      {error && (
        <span className="err" id={`${htmlFor}-err`} role="alert" style={{ margin: 0 }}>
          {error}
        </span>
      )}
    </div>
  );
}
