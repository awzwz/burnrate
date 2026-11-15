// Formatting only. Every amount on screen is computed by the server; these helpers turn
// cents, cents-per-day, basis points and ISO dates into text, and typed text back into integers.

const MONEY = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const WHOLE = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const MINUS = "−";

/** 123456 -> "$1,234.56". Negative amounts use a real minus sign. */
export function usd(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return "—";
  const sign = cents < 0 ? MINUS : "";
  return `${sign}$${MONEY.format(Math.abs(cents) / 100)}`;
}

/** 520000 -> "$5,200"; keeps cents when they are not zero: 17133 -> "$171.33". */
export function usdShort(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return "—";
  if (Math.round(cents) % 100 !== 0) return usd(cents);
  const sign = cents < 0 ? MINUS : "";
  return `${sign}$${WHOLE.format(Math.abs(cents) / 100)}`;
}

/** Cents per day as a float (738.3425) -> "$7.38". */
export function usdPerDay(centsPerDay: number | null | undefined): string {
  if (centsPerDay === null || centsPerDay === undefined || !Number.isFinite(centsPerDay)) return "—";
  return usd(Math.round(centsPerDay));
}

/** Signed change of a per-day value: -18.4 -> "−$0.18", 3 -> "+$0.03". */
export function usdDelta(centsPerDay: number): string {
  const rounded = Math.round(centsPerDay);
  if (rounded === 0) return "$0.00";
  return `${rounded < 0 ? MINUS : "+"}$${MONEY.format(Math.abs(rounded) / 100)}`;
}

/** The big counter is drawn as "$" + "7" + ".38". */
export function splitPerDay(centsPerDay: number): { whole: string; cents: string } {
  const rounded = Math.max(0, Math.round(centsPerDay));
  return { whole: WHOLE.format(Math.floor(rounded / 100)), cents: String(rounded % 100).padStart(2, "0") };
}

/** Fractions of a cent for the ticking line: 1.23 cents -> "$0.0123". */
export function usdTick(cents: number): string {
  if (!Number.isFinite(cents) || cents < 0) return "$0.0000";
  return `$${(cents / 100).toFixed(4)}`;
}

/** 2799 -> "27.99%". */
export function pct(bps: number | null | undefined): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) return "—";
  return `${(bps / 100).toFixed(2)}%`;
}

function parseISO(iso: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return null;
  // Noon local time keeps the calendar day stable in every time zone.
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12, 0, 0);
}

function fmt(iso: string | null | undefined, opts: Intl.DateTimeFormatOptions): string {
  if (!iso) return "—";
  const d = parseISO(iso);
  return d ? d.toLocaleDateString("en-US", opts) : "—";
}

/** "2026-11-20" -> "Fri, Nov 20". */
export const dateLong = (iso: string | null | undefined) => fmt(iso, { weekday: "short", month: "short", day: "numeric" });
/** "2026-11-20" -> "Nov 20". */
export const dateShort = (iso: string | null | undefined) => fmt(iso, { month: "short", day: "numeric" });
/** "2026-11-20" -> "Nov 20, 2026". */
export const dateFull = (iso: string | null | undefined) => fmt(iso, { month: "short", day: "numeric", year: "numeric" });
/** "2026-11-20" -> "Friday, Nov 20". */
export const dateWeekday = (iso: string | null | undefined) => fmt(iso, { weekday: "long", month: "short", day: "numeric" });

/** "2028-03" -> "Mar 2028". */
export function monthYear(ym: string | null | undefined): string {
  if (!ym) return "—";
  const m = /^(\d{4})-(\d{2})/.exec(ym);
  if (!m) return "—";
  return new Date(Number(m[1]), Number(m[2]) - 1, 1, 12).toLocaleDateString("en-US", { month: "short", year: "numeric" });
}

/** An ISO timestamp -> "Nov 20, 9:41 AM" in the viewer's time zone. */
export function dateTime(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** 9 -> "9th". */
export function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
}

/** 40 -> "3 years 4 months"; 7 -> "7 months". */
export function monthsText(months: number): string {
  const y = Math.floor(months / 12);
  const m = months % 12;
  const parts: string[] = [];
  if (y) parts.push(`${y} ${y === 1 ? "year" : "years"}`);
  if (m || !y) parts.push(`${m} ${m === 1 ? "month" : "months"}`);
  return parts.join(" ");
}

/** 84 -> "12 weeks", 7 -> "week", 30 -> "30 days". Used for "every 12 weeks". */
export function periodText(days: number): string {
  if (days % 7 === 0) {
    const w = days / 7;
    return w === 1 ? "week" : `${w} weeks`;
  }
  return days === 1 ? "day" : `${days} days`;
}

/** "https://www.capitalone.com/pay" -> "capitalone.com". */
export function hostLabel(url: string): string {
  try {
    return new URL(url.includes("://") ? url : `https://${url}`).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// ---------- typed text -> integers (format checks only) ----------

/** "5,200.00" or "$5200" -> 520000. Returns null when the text is not an amount. */
export function parseMoneyToCents(text: string): number | null {
  const clean = text.replace(/[$,\s]/g, "");
  if (!/^\d+(\.\d{0,2})?$|^\.\d{1,2}$/.test(clean)) return null;
  const [dollars = "0", frac = ""] = clean.split(".");
  return Number(dollars || "0") * 100 + Number((frac + "00").slice(0, 2));
}

/** "27.99" or "27.99%" -> 2799. Returns null when the text is not a rate. */
export function parseAprToBps(text: string): number | null {
  const clean = text.replace(/[%\s]/g, "");
  if (!/^\d{1,2}(\.\d{0,2})?$/.test(clean)) return null;
  const [whole = "0", frac = ""] = clean.split(".");
  return Number(whole) * 100 + Number((frac + "00").slice(0, 2));
}

/** 520000 -> "5,200.00" for putting a stored amount back into an input. */
export function centsToInput(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "";
  return MONEY.format(cents / 100);
}

/** 2799 -> "27.99". */
export function bpsToInput(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return "";
  return (bps / 100).toFixed(2);
}

/** A projection rounded to whole dollars: 96703 -> "$967". */
export function usdRound(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return "—";
  const sign = cents < 0 ? MINUS : "";
  return sign + "$" + WHOLE.format(Math.round(Math.abs(cents) / 100));
}

/** Today's date in the viewer's time zone as YYYY-MM-DD. */
export function localISODate(d: Date = new Date()): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** An ISO timestamp -> the calendar date in the viewer's time zone, as YYYY-MM-DD. */
export function tsToISODate(ts: string | null | undefined): string | null {
  if (!ts) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(ts)) return ts;
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? null : localISODate(d);
}
