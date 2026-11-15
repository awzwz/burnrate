import type { Meter } from "../api/types";
import { dateShort, usd } from "../lib/format";

/**
 * "Paid for itself?": what the user paid Burn Rate against what it found, in two separate lines.
 * Shown as it is, including when found is less than paid.
 */
export default function MeterBox({ meter, compact = false }: { meter: Meter; compact?: boolean }) {
  const ahead = meter.found_cents >= meter.paid_cents && meter.paid_cents > 0;
  const width = meter.paid_cents > 0 ? Math.min(100, Math.max(0, (meter.found_cents / meter.paid_cents) * 100)) : 0;
  return (
    <div className="pfi num" data-testid="meter">
      <div className="k">Paid for itself?</div>
      <div className="v">
        {ahead ? "Yes" : "Not yet"}: {usd(meter.found_cents)} found
        <span className="of">against {usd(meter.paid_cents)} paid to Burn Rate</span>
      </div>
      <div className="bar" aria-hidden="true">
        <i style={{ width: `${width}%` }} />
      </div>
      <dl>
        <dt>Confirmed by your bank</dt>
        <dd>{usd(meter.confirmed_cents)}</dd>
        <dt>Our estimate</dt>
        <dd>{usd(meter.estimated_cents)}</dd>
      </dl>
      {!compact && (
        <>
          {meter.items.length > 0 && (
            <ul className="items">
              {meter.items.map((it, i) => (
                <li key={`${it.date}-${i}`}>
                  <span>
                    {it.label.replace(/\s#\S+$/, "")}
                    <small>
                      {dateShort(it.date)} · {it.kind === "confirmed" ? "confirmed by your bank" : "our estimate"}
                    </small>
                  </span>
                  <span>{usd(it.cents)}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="fineprint">
            Estimates compare paying on payday with paying on the due date. A confirmed line is something your bank's statement or rep
            agreed to.
          </p>
        </>
      )}
    </div>
  );
}
