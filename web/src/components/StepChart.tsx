import type { BurnPoint } from "../api/types";
import { dateShort, usdPerDay } from "../lib/format";

const W = 326;
const H = 92;
const PAD_R = 60;
const TOP = 10;
const BOT = 18;

function dayNumber(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  return Date.UTC(y, m - 1, d) / 86400000;
}

/**
 * Burn per day as a step line: the value holds until the next dated event changes it.
 * Positions are a drawing of the server's history; no amount is computed here.
 */
export default function StepChart({ history, today, now, good }: { history: BurnPoint[]; today: string; now: number; good?: boolean }) {
  if (!history.length) return null;
  const first = history[0];
  const t0 = dayNumber(first.date);
  const t1 = Math.max(dayNumber(today), dayNumber(history[history.length - 1].date));
  const span = Math.max(1, t1 - t0);
  const values = [...history.map((p) => p.value), now];
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (hi - lo < 1) {
    hi += 1;
    lo -= 1;
  }
  const pad = (hi - lo) * 0.18;
  lo -= pad;
  hi += pad;
  const endX = W - PAD_R + 14;
  const x = (iso: string) => ((dayNumber(iso) - t0) / span) * endX;
  const y = (v: number) => TOP + ((hi - v) / (hi - lo)) * (H - TOP - BOT);

  let d = `M0 ${y(first.value).toFixed(1)}`;
  for (let i = 1; i < history.length; i++) d += ` H${x(history[i].date).toFixed(1)} V${y(history[i].value).toFixed(1)}`;
  d += ` H${endX} V${y(now).toFixed(1)}`;
  const mid = y((hi + lo) / 2);
  const lastDate = history[history.length - 1].date;
  const endsToday = lastDate === today;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Burn per day from ${dateShort(first.date)} to ${dateShort(today)}: ${usdPerDay(first.value)} at the start, ${usdPerDay(now)} now.`}>
      <line className="ch-grid" x1="0" x2={W} y1={mid} y2={mid} strokeDasharray="2 4" />
      <path className="ch-line" d={d} fill="none" strokeWidth="2" strokeLinejoin="round" />
      {history.map((p, i) =>
        i === 0 || (i === history.length - 1 && endsToday) ? null : <circle key={`${p.date}-${i}`} className="ch-node" cx={x(p.date)} cy={y(p.value)} r="3" strokeWidth="2" />,
      )}
      <circle className={`ch-end ${good ? "ok" : ""}`} cx={endX} cy={y(now)} r="4" />
      <text className="ch-val" x={W} y={y(now) + 4} textAnchor="end" fontSize="12" fontWeight="600" style={{ fontVariantNumeric: "tabular-nums" }}>
        {usdPerDay(now)}
      </text>
      <text className="ch-ax" x="0" y={H - 2} fontSize="11">
        {dateShort(first.date)}
      </text>
      <text className="ch-ax" x={endX} y={H - 2} textAnchor="middle" fontSize="11">
        Today
      </text>
    </svg>
  );
}
