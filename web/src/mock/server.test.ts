import { beforeEach, describe, expect, it } from "vitest";
import { readEventStream } from "../api/client";
import type { CardBurn, Me, Rehearsal, Result, Today } from "../api/types";
import { handle } from "./server";

// The mock stands in for docs/API.md. These tests pin the sample persona's numbers and the error envelope.
async function call<T>(method: string, path: string, body?: unknown): Promise<{ status: number; data: T }> {
  const res = await handle(new URL(`http://localhost/api${path}`), { method, body: body === undefined ? undefined : JSON.stringify(body) });
  return { status: res.status, data: res.status === 202 ? (undefined as T) : ((await res.json()) as T) };
}

beforeEach(async () => {
  localStorage.clear();
  await call("POST", "/auth/logout");
  await call("POST", "/demo/start", { persona: "aziz" });
});

describe("mock API: the sample persona", () => {
  it("burns $7.38 a day and sends $240.00 to Capital One Quicksilver on the first payday", async () => {
    const { data: today } = await call<Today>("GET", "/today");
    expect(today.headline).toBe("It's payday.");
    expect(Math.round(today.burn.now)).toBe(738);
    expect(today.instruction?.status).toBe("due");
    expect(today.instruction?.sends[0]).toMatchObject({ card_name: "Capital One Quicksilver", amount_cents: 24000, covers_min: true, min_cents: 17133, extra_cents: 6867, saved_by_date_cents: 350 });
    expect(today.meter).toMatchObject({ paid_cents: 3999, confirmed_cents: 317, estimated_cents: 699 });
  });

  it("marks as paid, lowers the burn, and undoes it", async () => {
    const { data: before } = await call<Today>("GET", "/today");
    const id = before.instruction!.id;
    const { data: paid } = await call<Today>("POST", `/plan/instructions/${id}/paid`, {});
    expect(paid.headline).toBe("Done for this payday.");
    expect(paid.instruction).toMatchObject({ status: "paid", paid_cents: 24000 });
    expect(Math.round(paid.burn.now)).toBe(720);
    const { data: undone } = await call<Today>("POST", `/plan/instructions/${id}/undo`);
    expect(undone.instruction?.status).toBe("due");
    expect(Math.round(undone.burn.now)).toBe(738);
    expect(undone.burn.history).toHaveLength(before.burn.history.length);
  });

  it("recalculates a short payday without dropping reserved minimums", async () => {
    const { data: before } = await call<Today>("GET", "/today");
    const { data: short } = await call<Today>("POST", `/plan/instructions/${before.instruction!.id}/short`, { available_cents: 15000 });
    expect(short.instruction).toMatchObject({ status: "short", total_send_cents: 9000, short_by_cents: 15000, reserved_cents: 6000 });
  });

  it("moves the demo clock to the next payday", async () => {
    const { data } = await call<{ me: Me; jumped_to: string; note: string }>("POST", "/demo/clock", { action: "next_payday" });
    expect(data.me.demo?.today).toBe(data.jumped_to);
    expect(data.note).toContain("Autopay paid $60.00 to Discover it");
    const { data: today } = await call<Today>("GET", "/today");
    expect(today.headline).toBe("It's payday.");
    expect(today.date).toBe(data.jumped_to);
  });

  it("answers the live card line and the quiz result for the sample numbers", async () => {
    const { data: burn } = await call<CardBurn>("POST", "/quiz/card-burn", { balance_cents: 520000, apr_bps: 2799 });
    expect(Math.round(burn.burn_per_day)).toBe(399);
    await call("POST", "/quiz/sample");
    const { data: result } = await call<Result>("POST", "/quiz/result");
    expect(Math.round(result.burn_per_day)).toBe(738);
    expect(result.per_paycheck_cents).toBe(10337);
    expect(result.per_year_cents).toBe(269495);
    expect(result.first_move).toMatchObject({ amount_cents: 24000, card_name: "Capital One Quicksilver" });
  });

  it("uses the documented error envelope", async () => {
    const bad = await call<{ error: { code: string; message: string } }>("POST", "/billing/checkout", { plan_id: "w12", email: "maya@example.com", consent_renewal: false });
    expect(bad.status).toBe(422);
    expect(bad.data.error.code).toBe("invalid_input");
    expect(bad.data.error.message).toMatch(/tick the box/);
    await call("POST", "/auth/logout");
    const locked = await call<{ error: { code: string } }>("GET", "/today");
    expect(locked.status).toBe(402);
    expect(locked.data.error.code).toBe("subscription_required");
  });

  it("streams a rehearsal turn as server-sent events and ticks the checklist", async () => {
    const { data: asks } = await call<{ asks: { id: string; type: string; status: string }[] }>("GET", "/asks");
    const ask = asks.asks.find((a) => a.type === "apr_reduction" && a.status === "queued")!;
    const { data: rh } = await call<Rehearsal>("POST", "/rehearsal", { ask_id: ask.id });
    expect(rh.messages).toHaveLength(1);
    const res = await handle(new URL(`http://localhost/api/rehearsal/${rh.id}/turn`), { method: "POST", body: JSON.stringify({ text: "Will this request result in a hard inquiry?" }) });
    expect(res.headers.get("Content-Type")).toBe("text/event-stream");
    let streamed = "";
    let final: Rehearsal | null = null;
    await readEventStream(res, (event, data) => {
      if (event === "delta") streamed += (JSON.parse(data) as { text: string }).text;
      if (event === "done") final = JSON.parse(data) as Rehearsal;
    });
    expect(final).not.toBeNull();
    const done = final as unknown as Rehearsal;
    expect(done.messages).toHaveLength(3);
    expect(done.messages[2].text).toBe(streamed);
    expect(done.checklist.find((c) => c.id === "inquiry")?.done).toBe(true);
  });
});
