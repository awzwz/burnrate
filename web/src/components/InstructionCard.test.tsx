import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Instruction } from "../api/types";
import InstructionCard, { explain } from "./InstructionCard";

function instruction(over: Partial<Instruction> = {}, sendOver: Partial<Instruction["sends"][number]> = {}): Instruction {
  return {
    id: "ins_1",
    payday: "2026-11-20",
    next_payday: "2026-12-04",
    status: "due",
    sends: [
      {
        card_id: "c1",
        card_name: "Capital One Quicksilver",
        issuer: "capital_one",
        last4: "4417",
        apr_bps: 2799,
        amount_cents: 24000,
        covers_min: true,
        min_cents: 17133,
        extra_cents: 6867,
        due: "2026-12-09",
        days_early: 19,
        saved_by_date_cents: 350,
        burn_cut: 18.404,
        pay_url: "https://www.capitalone.com",
        ...sendOver,
      },
    ],
    reserved_cents: 6000,
    short_by_cents: 0,
    strategy: "avalanche",
    reason: "Capital One Quicksilver has your highest rate.",
    burn_before: 738.3425,
    burn_after: 719.9385,
    total_send_cents: 24000,
    paid_at: null,
    paid_cents: null,
    ...over,
  };
}

const noop = () => undefined;

describe("InstructionCard", () => {
  it("shows one amount, one card and one day", () => {
    render(<InstructionCard instruction={instruction()} today="2026-11-20" highestRateCardIds={["c1"]} onPaid={noop} onUndo={noop} onShort={noop} />);
    expect(screen.getByTestId("today-label")).toHaveTextContent("Send today");
    expect(screen.getByTestId("today-amount")).toHaveTextContent("$240.00");
    expect(screen.getByTestId("today-card")).toHaveTextContent("Capital One Quicksilver");
    expect(screen.getByText("Fri, Nov 20")).toBeInTheDocument();
    expect(screen.getByText(/27\.99% APR, your highest/)).toBeInTheDocument();
  });

  it("builds the explanation from the instruction's own fields", () => {
    render(<InstructionCard instruction={instruction()} today="2026-11-20" highestRateCardIds={["c1"]} onPaid={noop} onUndo={noop} onShort={noop} />);
    const card = screen.getByRole("region", { name: "Payday instruction" });
    expect(card).toHaveTextContent("Covers its $171.33 minimum due Dec 9 and puts $68.67 extra on your highest rate.");
    expect(card).toHaveTextContent("Paying today instead of on the due date saves about $3.50 in interest.");
  });

  it("does not call the card the highest rate when it is not", () => {
    render(<InstructionCard instruction={instruction({ reason: "This card can be cleared in two paychecks." })} today="2026-11-20" highestRateCardIds={["c2"]} onPaid={noop} onUndo={noop} onShort={noop} />);
    const card = screen.getByRole("region", { name: "Payday instruction" });
    expect(card).not.toHaveTextContent("your highest");
    expect(card).toHaveTextContent("puts $68.67 extra on this card");
    expect(card).toHaveTextContent("This card can be cleared in two paychecks.");
  });

  it("says so plainly when the amount is below the minimum", () => {
    const text = explain(instruction({}, { covers_min: false, amount_cents: 9000, extra_cents: 0, saved_by_date_cents: 0 }).sends[0], "short", true);
    expect(text.main.join("")).toBe("This is less than its $171.33 minimum due Dec 9. Keep autopay on so the minimum is paid on time.");
    expect(text.saved).toBeNull();
  });

  it("links to the issuer's site in a new tab", () => {
    render(<InstructionCard instruction={instruction()} today="2026-11-20" onPaid={noop} onUndo={noop} onShort={noop} />);
    const link = screen.getByTestId("today-pay-link");
    expect(link).toHaveTextContent("Pay at capitalone.com");
    expect(link).toHaveAttribute("href", "https://www.capitalone.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("leaves the pay link out when the server sends no URL", () => {
    render(<InstructionCard instruction={instruction({}, { pay_url: "" })} today="2026-11-20" onPaid={noop} onUndo={noop} onShort={noop} />);
    expect(screen.queryByTestId("today-pay-link")).not.toBeInTheDocument();
    expect(screen.getByTestId("today-paid")).toBeInTheDocument();
  });

  it("marks as paid with the instruction's own amount", async () => {
    const onPaid = vi.fn();
    render(<InstructionCard instruction={instruction()} today="2026-11-20" onPaid={onPaid} onUndo={noop} onShort={noop} />);
    await userEvent.click(screen.getByTestId("today-paid"));
    expect(onPaid).toHaveBeenCalledTimes(1);
    expect(onPaid).toHaveBeenCalledWith();
  });

  it("lets the user report a different amount", async () => {
    const onPaid = vi.fn();
    render(<InstructionCard instruction={instruction()} today="2026-11-20" onPaid={onPaid} onUndo={noop} onShort={noop} />);
    await userEvent.click(screen.getByTestId("today-other-amount"));
    await userEvent.type(screen.getByTestId("today-inline-amount"), "200");
    await userEvent.click(screen.getByTestId("today-inline-save"));
    expect(onPaid).toHaveBeenCalledWith(20000);
  });

  it("asks how much is available on a short payday and sends it in cents", async () => {
    const onShort = vi.fn();
    render(<InstructionCard instruction={instruction()} today="2026-11-20" onPaid={noop} onUndo={noop} onShort={onShort} />);
    expect(screen.queryByTestId("today-inline-amount")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("today-short"));
    expect(screen.getByLabelText("How much can go to your cards this payday?")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("today-inline-save"));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter an amount");
    expect(onShort).not.toHaveBeenCalled();
    await userEvent.type(screen.getByTestId("today-inline-amount"), "150.50");
    await userEvent.click(screen.getByTestId("today-inline-save"));
    expect(onShort).toHaveBeenCalledWith(15050);
  });

  it("explains a short payday without blame", () => {
    render(<InstructionCard instruction={instruction({ status: "short", short_by_cents: 15000 }, { amount_cents: 9000 })} today="2026-11-20" onPaid={noop} onUndo={noop} onShort={noop} />);
    expect(screen.getByTestId("today-short-note")).toHaveTextContent("$150.00 less than the usual plan");
    expect(screen.getByTestId("today-short-note")).toHaveTextContent("Nothing is marked as missed");
    expect(screen.getByTestId("today-paid")).toBeInTheDocument();
  });

  it("shows the paid state with Undo and the amount that was paid", async () => {
    const onUndo = vi.fn();
    render(<InstructionCard instruction={instruction({ status: "paid", paid_at: "2026-11-20T15:00:00Z", paid_cents: 20000 })} today="2026-11-20" onPaid={noop} onUndo={onUndo} onShort={noop} />);
    expect(screen.getByTestId("today-label")).toHaveTextContent("Paid today");
    expect(screen.getByTestId("today-amount")).toHaveTextContent("$200.00");
    expect(screen.queryByTestId("today-paid")).not.toBeInTheDocument();
    expect(screen.queryByTestId("today-short")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("today-undo"));
    expect(onUndo).toHaveBeenCalledTimes(1);
  });

  it("previews the next payday without a Mark as paid button", () => {
    render(<InstructionCard instruction={instruction({ status: "upcoming", payday: "2026-12-04", next_payday: "2026-12-18" })} today="2026-11-30" onPaid={noop} onUndo={noop} onShort={noop} />);
    expect(screen.getByTestId("today-label")).toHaveTextContent("Next payday");
    expect(screen.queryByTestId("today-paid")).not.toBeInTheDocument();
    expect(screen.queryByTestId("today-short")).not.toBeInTheDocument();
    const card = screen.getByRole("region", { name: "Payday instruction" });
    expect(within(card).getByText("Fri, Dec 4")).toBeInTheDocument();
    expect(card).toHaveTextContent("Paying on payday instead of on the due date");
  });

  it("disables the buttons while a request is running and shows a server error", () => {
    render(<InstructionCard instruction={instruction()} today="2026-11-20" busy error="This instruction is no longer current." onPaid={noop} onUndo={noop} onShort={noop} />);
    expect(screen.getByTestId("today-paid")).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("This instruction is no longer current.");
  });
});
