import type { Subscription } from "../api/types";
import { dateShort, tsToISODate } from "./format";

/** "12-week plan · renews Jan 1" for the account line. Wording follows the subscription state the server reports. */
export function planLine(sub: Subscription): string {
  const end = dateShort(tsToISODate(sub.current_period_end));
  if (sub.status === "paused") return `${sub.plan.name} · paused${sub.paused_until ? ` until ${dateShort(sub.paused_until)}` : ""}`;
  if (sub.status === "refunded") return `${sub.plan.name} · refunded`;
  if (sub.status === "past_due") return `${sub.plan.name} · payment needed`;
  if (sub.status === "canceled" || sub.cancel_at_period_end) return `${sub.plan.name} · ends ${end}`;
  return `${sub.plan.name} · renews ${end}`;
}
