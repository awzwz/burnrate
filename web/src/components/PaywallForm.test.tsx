import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Plan } from "../api/types";
import PaywallForm from "./PaywallForm";

const PLANS: Plan[] = [
  { id: "w1", name: "1-week plan", price_cents: 693, days: 7, renews_price_cents: 693, renews_days: 7, per_day_cents: 99, then: "then $6.93 every week", tag: null },
  { id: "w4", name: "4-week plan", price_cents: 1999, days: 28, renews_price_cents: 3999, renews_days: 28, per_day_cents: 71.39, then: "then $39.99 every 4 weeks", tag: null },
  { id: "w12", name: "12-week plan", price_cents: 3999, days: 84, renews_price_cents: 7999, renews_days: 84, per_day_cents: 47.61, then: "then $79.99 every 12 weeks", tag: "Full program" },
];

function setup(onStart = vi.fn().mockResolvedValue(undefined)) {
  render(<PaywallForm plans={PLANS} guaranteeDays={14} burnPerDay={738.3425} testMode onStart={onStart} />);
  return { onStart, user: userEvent.setup() };
}

describe("PaywallForm", () => {
  it("preselects the tagged 12-week plan and prices the button from it", () => {
    setup();
    expect(screen.getByRole("radio", { name: /12-week plan/ })).toBeChecked();
    expect(screen.getByTestId("plan-cta")).toHaveTextContent("Start for $39.99");
    expect(screen.getByText(/Your cards burn \$7\.38 a day/)).toBeInTheDocument();
    expect(screen.getByTestId("plan-per-day")).toHaveTextContent("$0.48");
  });

  it("shows the renewal terms inside every option", () => {
    setup();
    expect(screen.getByTestId("plan-option-w1")).toHaveTextContent("then $6.93 every week");
    expect(screen.getByTestId("plan-option-w4")).toHaveTextContent("then $39.99 every 4 weeks");
    expect(screen.getByTestId("plan-option-w12")).toHaveTextContent("then $79.99 every 12 weeks");
  });

  it("starts with the consent box unticked and names the exact renewal price and period", () => {
    setup();
    expect(screen.getByTestId("plan-consent")).not.toBeChecked();
    expect(screen.getByTestId("plan-consent-text")).toHaveTextContent(
      "I understand that after the first 12 weeks for $39.99 the plan renews automatically at $79.99 every 12 weeks until I cancel.",
    );
  });

  it("refuses to start without consent and highlights the box", async () => {
    const { onStart, user } = setup();
    await user.type(screen.getByTestId("plan-email"), "maya@example.com");
    await user.click(screen.getByTestId("plan-cta"));
    expect(onStart).not.toHaveBeenCalled();
    const box = screen.getByTestId("plan-consent");
    expect(box.closest("label")).toHaveClass("need");
    expect(box).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("Please tick the box to confirm the renewal terms.");
  });

  it("clears the highlight and proceeds once the box is ticked", async () => {
    const { onStart, user } = setup();
    await user.type(screen.getByTestId("plan-email"), "maya@example.com");
    await user.click(screen.getByTestId("plan-cta"));
    await user.click(screen.getByTestId("plan-consent"));
    expect(screen.getByTestId("plan-consent").closest("label")).not.toHaveClass("need");
    await user.click(screen.getByTestId("plan-cta"));
    expect(onStart).toHaveBeenCalledTimes(1);
    expect(onStart).toHaveBeenCalledWith("w12", "maya@example.com");
  });

  it("asks for an email before anything else", async () => {
    const { onStart, user } = setup();
    await user.click(screen.getByTestId("plan-consent"));
    await user.click(screen.getByTestId("plan-cta"));
    expect(onStart).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your email address");
    expect(screen.getByTestId("plan-email")).toHaveAttribute("aria-invalid", "true");
  });

  it("rewrites the consent for another plan and asks for it again", async () => {
    const { onStart, user } = setup();
    await user.click(screen.getByTestId("plan-consent"));
    expect(screen.getByTestId("plan-consent")).toBeChecked();
    await user.click(screen.getByRole("radio", { name: /4-week plan/ }));
    expect(screen.getByTestId("plan-cta")).toHaveTextContent("Start for $19.99");
    expect(screen.getByTestId("plan-consent-text")).toHaveTextContent("after the first 4 weeks for $19.99 the plan renews automatically at $39.99 every 4 weeks");
    expect(screen.getByTestId("plan-per-day")).toHaveTextContent("$0.71");
    expect(screen.getByTestId("plan-consent")).not.toBeChecked();
    await user.type(screen.getByTestId("plan-email"), "maya@example.com");
    await user.click(screen.getByTestId("plan-cta"));
    expect(onStart).not.toHaveBeenCalled();
    await user.click(screen.getByTestId("plan-consent"));
    await user.click(screen.getByTestId("plan-cta"));
    expect(onStart).toHaveBeenCalledWith("w4", "maya@example.com");
  });

  it("words a one-week plan correctly", async () => {
    const { user } = setup();
    await user.click(screen.getByRole("radio", { name: /1-week plan/ }));
    expect(screen.getByTestId("plan-consent-text")).toHaveTextContent("after the first week for $6.93 the plan renews automatically at $6.93 every week");
  });

  it("shows the money-back line from the API and no countdown or struck-through price", () => {
    setup();
    expect(screen.getByText(/14-day money-back\. No conditions\./)).toBeInTheDocument();
    expect(document.querySelector("s, del, strike")).toBeNull();
    expect(document.body.textContent).not.toMatch(/\b\d{1,2}:\d{2}\b/);
  });

  it("leaves the burn comparison out when there is no quiz result", () => {
    render(<PaywallForm plans={PLANS} guaranteeDays={14} burnPerDay={null} testMode={false} onStart={vi.fn()} />);
    expect(screen.queryByText(/Your cards burn/)).not.toBeInTheDocument();
    expect(screen.getByText(/This plan costs/)).toBeInTheDocument();
  });
});
