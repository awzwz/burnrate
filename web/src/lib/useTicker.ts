import { useEffect, useState } from "react";

/**
 * Interest "burned" since `sinceMs`, in cents, redrawn four times a second.
 * This is a visualisation of the server's burn_per_day against the wall clock, as the result screen specifies;
 * the bank itself charges once a day.
 */
export function useTicker(burnPerDay: number | null, sinceMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (burnPerDay === null) return;
    const id = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [burnPerDay]);
  if (burnPerDay === null) return 0;
  return (Math.max(0, now - sinceMs) * burnPerDay) / 86400000;
}

const KEY = "br-quiz-start";

/** When this tab first opened the quiz. Stored in sessionStorage so the result screen can count from it. */
export function quizStart(createIfMissing: boolean): number {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw && Number(raw) > 0) return Number(raw);
    if (createIfMissing) sessionStorage.setItem(KEY, String(Date.now()));
  } catch {
    /* private mode: fall through to "now" */
  }
  return Date.now();
}
