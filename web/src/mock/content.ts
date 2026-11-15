// Mock fixture text for the ask sprint: scripts, call cards and the simulated rep.
// On the real server these come from the asks service with the user's own numbers.
import type { AskDetail, AskType, CallNode } from "../api/types";
import { dateShort, ordinal, pct, usd, usdPerDay, usdShort } from "../lib/format";
import { burnOf, issuerName } from "./engine";

export interface AskCard {
  id: string;
  name: string;
  issuer: string;
  last4: string;
  balance_cents: number;
  apr_bps: number;
  due_day: number;
}

export interface MustAsk {
  id: string;
  label: string;
  /** Words that count as "asked" in the practice chat. */
  match: string;
  /** What the simulated rep answers. */
  answer: string;
}

export interface MockAsk extends AskDetail {
  must: MustAsk[];
  rep: { scenario: string; opening: string; beats: string[]; closing: string };
}

const NAME_Q: MustAsk = {
  id: "name",
  label: "Can I have your name for my notes?",
  match: "your name|who am i speaking|name for my",
  answer: "Of course. My name is Jordan.",
};

function lateFee(card: AskCard, feeCents: number, order: number): MockAsk {
  const bank = issuerName(card);
  const fee = usd(feeCents);
  const must: MustAsk[] = [
    { id: "when", label: "When will the refund show on my statement?", match: "when will|when does|when would|when can|how long|how soon", answer: `The ${fee} credit posts in one to two billing cycles and will show on your next statement.` },
    NAME_Q,
  ];
  const nodes: Record<string, CallNode> = {
    start: {
      say: `Hi, I'm calling about my ${card.name} card${card.last4 ? ` ending in ${card.last4}` : ""}. My last statement has a ${fee} late fee. I pay on time and autopay is set up now. Could you refund that fee as a one-time courtesy?`,
      hint: "Call the number on the back of your card. Say it once, then wait.",
      options: [
        { label: "They agreed to refund it", next: "granted" },
        { label: "They offered part of it", next: "partial" },
        { label: "They said no", next: "refused" },
        { label: "They are transferring me", next: "transfer" },
      ],
    },
    transfer: {
      say: `Thanks for taking my call. There is a ${fee} late fee on my last statement. I've set up autopay so it won't repeat. Could you remove the fee as a courtesy?`,
      hint: "A second person often has more room to say yes. Repeat the ask in full.",
      options: [
        { label: "They agreed to refund it", next: "granted" },
        { label: "They said no", next: "refused" },
      ],
    },
    partial: {
      say: "Thank you. Is there any way to refund the full amount, since this was a one-time miss?",
      options: [
        { label: "Now the full fee", next: "granted" },
        { label: "Still only part", next: "partial_done" },
      ],
    },
    refused: {
      say: "I understand. Is there anything on my account that would let you waive it, or could I speak with a supervisor?",
      hint: "A calm second ask costs nothing.",
      options: [
        { label: "They are transferring me", next: "transfer" },
        { label: "Still no", next: "refused_done" },
      ],
    },
    granted: {
      say: "Thank you. When will the credit show on my statement? And can I have your name for my notes?",
      hint: "Write down the name and today's date, then log it.",
      options: [],
      outcome: "granted",
    },
    partial_done: {
      say: "Thank you, I'll take that. When will the credit show on my statement? And can I have your name for my notes?",
      options: [],
      outcome: "partial",
    },
    refused_done: {
      say: "Thanks for checking. When would be a good time to ask again? Can I have your name for my notes?",
      hint: "A no is a logged result, not a failure. Your plan does not change.",
      options: [],
      outcome: "refused",
    },
  };
  return {
    id: `ask_${card.id}_late_fee`,
    type: "late_fee_refund",
    card_id: card.id,
    card_name: card.name,
    issuer: card.issuer,
    title: `${bank} · late-fee refund`,
    subtitle: "The fee on your last statement",
    value_cents: feeCents,
    value_label: fee,
    status: "queued",
    order,
    result: null,
    why: `A ${fee} late fee posted on your ${card.name} statement. If ${bank} refunds it, your balance drops by ${fee} and that amount stops collecting interest. You ask once; it takes a few minutes.`,
    evidence: {
      text: "In a 2026 LendingTree survey, most cardholders who asked to have a late fee waived said the issuer agreed.",
      source: "LendingTree survey, 2026, as reported in the press. Self-reported answers.",
      caveat: `The survey counts people who chose to ask. It says nothing certain about your account, and ${bank} may say no.`,
    },
    script: {
      opening: `Hi, I'm calling about my ${card.name} card${card.last4 ? ` ending in ${card.last4}` : ""}.`,
      lines: [
        { label: "Your history", text: "I've had this card for a while and I pay on time. Autopay is set up now, so this won't repeat." },
        { label: "The ask", text: `My last statement has a ${fee} late fee. Could you refund it as a one-time courtesy?` },
        { label: "If yes", text: `Thank you. When will the ${fee} credit show on my statement?` },
        { label: "Before you hang up", text: "Can I have your name for my notes?" },
      ],
      must_ask: must.map((m) => m.label),
      if_refused: "I understand. Is there anything on my account that would let you waive it, or a date I could ask again? Could I speak with a supervisor?",
    },
    written: `Hello,\n\nMy ${card.name} statement${card.last4 ? ` (card ending in ${card.last4})` : ""} shows a ${fee} late fee. I pay on time and have now set up autopay, so this will not repeat. Could you refund the ${fee} fee as a one-time courtesy?\n\nPlease confirm when the credit will show on my statement.\n\nThank you.`,
    call_card: { start: "start", nodes },
    playbook: {
      count: 3,
      entries: [
        { channel: "phone", result: "granted", note: "Refunded on the first ask after mentioning autopay.", month: "2026-10" },
        { channel: "chat", result: "granted", note: "Chat agent applied the credit in a few minutes.", month: "2026-10" },
        { channel: "phone", result: "refused", note: "Second late fee in six months; told to ask again after three on-time payments.", month: "2026-09" },
      ],
    },
    must,
    rep: {
      scenario: "The rep can give a one-time courtesy refund after checking your payment history.",
      opening: `Thank you for calling ${bank} card services, this is Jordan. How can I help you today?`,
      beats: [
        `I can take a look. I do see a ${fee} late fee on the last statement. Can you tell me what happened with that payment?`,
        "Thanks for explaining. I see on-time payments before that one. I can remove the fee as a one-time courtesy. Is there anything you'd like to ask before I apply it?",
      ],
      closing: `You're all set. The ${fee} credit is noted on the account. Thanks for calling ${bank}.`,
    },
  };
}

function aprReduction(card: AskCard, order: number): MockAsk {
  const bank = issuerName(card);
  const perPoint = usdShort(Math.round(card.balance_cents / 100 / 100) * 100);
  const offer = Math.max(0, card.apr_bps - 300);
  const must: MustAsk[] = [
    { id: "permanent", label: "Is the new rate permanent or temporary, and until what date?", match: "permanent|temporary|how long|until|expire", answer: `It is temporary: ${pct(offer)} for the next 12 billing cycles. After that the standard rate returns.` },
    { id: "balances", label: "Does it apply to my existing balance?", match: "existing|current balance|which balance|balances|new purchases", answer: "It applies to your existing purchase balance and to new purchases." },
    { id: "inquiry", label: "Will this request result in a hard inquiry?", match: "hard inquiry|hard pull|credit check|credit report|inquiry", answer: "No. This review does not involve a credit check." },
    NAME_Q,
  ];
  const nodes: Record<string, CallNode> = {
    start: {
      say: `Hi, I'm calling about my ${card.name} card${card.last4 ? ` ending in ${card.last4}` : ""}. My purchase APR is ${pct(card.apr_bps)}. I pay on time, and I'd like to ask for a lower rate. Before you check: will this request result in a hard inquiry?`,
      hint: "Call the number on the back of your card. Ask about the inquiry before agreeing to any review.",
      options: [
        { label: "No hard inquiry, they are checking", next: "checking" },
        { label: "It would be a hard inquiry", next: "inquiry" },
        { label: "They are transferring me", next: "transfer" },
      ],
    },
    inquiry: {
      say: "Then please don't run it. Is there a rate review that doesn't need a credit check?",
      options: [
        { label: "Yes, without a check", next: "checking" },
        { label: "No other option", next: "refused_done" },
      ],
    },
    transfer: {
      say: `Thanks for taking my call. My APR is ${pct(card.apr_bps)} and I pay on time. I'd like a lower rate. Will this request result in a hard inquiry?`,
      hint: "This may be the retention team. Repeat the ask in full.",
      options: [
        { label: "No hard inquiry, they are checking", next: "checking" },
        { label: "They said no", next: "refused" },
      ],
    },
    checking: {
      say: "Thank you. I'll hold.",
      hint: "Wait for the answer. Silence is fine.",
      options: [
        { label: "They offered a lower rate", next: "offer" },
        { label: "They offered a temporary rate", next: "temporary" },
        { label: "They said no", next: "refused" },
      ],
    },
    offer: {
      say: "Thank you. Is that rate permanent? Does it apply to my existing balance? And what is the new rate exactly?",
      hint: "Write down the new rate, then log it.",
      options: [
        { label: "Permanent", next: "granted" },
        { label: "Only for some months", next: "temporary" },
      ],
    },
    temporary: {
      say: "Thank you. Until what date does it last, what rate applies afterwards, and does it cover my existing balance?",
      hint: "Write down the rate and the end date. We will remind you before it ends.",
      options: [],
      outcome: "temporary",
    },
    refused: {
      say: "I understand. What would make my account eligible, and when should I ask again? Could I speak with someone in retention?",
      options: [
        { label: "They are transferring me", next: "transfer" },
        { label: "Still no", next: "refused_done" },
      ],
    },
    granted: {
      say: "Thank you. Can I have your name for my notes? When does the new rate start?",
      options: [],
      outcome: "granted",
    },
    refused_done: {
      say: "Thanks for checking. Can I have your name for my notes?",
      hint: "A no is a logged result, not a failure. Your payday plan does not change.",
      options: [],
      outcome: "refused",
    },
  };
  return {
    id: `ask_${card.id}_apr`,
    type: "apr_reduction",
    card_id: card.id,
    card_name: card.name,
    issuer: card.issuer,
    title: `${bank} · lower rate`,
    subtitle: `Each point off is worth ${perPoint} a year on this balance`,
    value_cents: null,
    value_label: pct(card.apr_bps),
    status: "queued",
    order,
    result: null,
    why: `${card.name} carries ${usd(card.balance_cents)} at ${pct(card.apr_bps)}. It costs ${usdPerDay(burnOf(card))} a day. Each percentage point off the rate is worth about ${perPoint} a year at this balance, so even a small or temporary cut shows up in your daily number.`,
    evidence: {
      text: "In a 2026 LendingTree survey, most cardholders who asked their issuer for a lower rate said they got one.",
      source: "LendingTree survey, 2026, as reported in the press. Self-reported answers.",
      caveat: `The survey counts people who chose to ask and does not say whether the cuts were permanent. ${bank} may say no, and your plan works either way.`,
    },
    script: {
      opening: `Hi, I'm calling about my ${card.name} card${card.last4 ? ` ending in ${card.last4}` : ""}.`,
      lines: [
        { label: "Your history", text: "I've been a customer for a while and I pay on time." },
        { label: "The ask", text: `My purchase APR is ${pct(card.apr_bps)}. I'd like to ask for a lower rate.` },
        { label: "Before they check", text: "Will this request result in a hard inquiry?" },
        { label: "If they offer a rate", text: "Is it permanent or temporary, and until what date? Does it apply to my existing balance?" },
        { label: "Before you hang up", text: "Can I have your name for my notes?" },
      ],
      must_ask: must.map((m) => m.label),
      if_refused: "I understand. What would make my account eligible for a lower rate, and when should I ask again?",
    },
    written: `Hello,\n\nI have been a ${bank} customer for a while and I pay on time. The purchase APR on my ${card.name} card${card.last4 ? ` (ending in ${card.last4})` : ""} is ${pct(card.apr_bps)}. I would like to ask for a lower rate.\n\nBefore any review, please tell me whether it involves a hard inquiry. If you can offer a rate, please tell me whether it is permanent or temporary, until what date, and whether it applies to my existing balance.\n\nThank you.`,
    call_card: { start: "start", nodes },
    playbook: {
      count: 4,
      entries: [
        { channel: "phone", result: "temporary", note: "Three points off for 12 billing cycles. No credit check.", month: "2026-10" },
        { channel: "phone", result: "refused", note: "Told to ask again in six months.", month: "2026-10" },
        { channel: "chat", result: "refused", note: "Chat could not review rates; asked to call.", month: "2026-09" },
        { channel: "phone", result: "granted", note: "Two points off after a transfer to retention.", month: "2026-09" },
      ],
    },
    must,
    rep: {
      scenario: "The rep cannot change the standard rate but can offer a temporary lower rate.",
      opening: `Thank you for calling ${bank}, this is Jordan. How can I help you today?`,
      beats: [
        "I can review the rate on your account. Is there a reason you're asking today?",
        `Thanks for waiting. I can't change the standard rate, but I can offer ${pct(offer)} for the next 12 billing cycles. Would you like me to apply it?`,
      ],
      closing: "Done. The offer is applied and starts with your next statement. Thanks for calling.",
    },
  };
}

function dueDateMove(card: AskCard, nextDue: string, paydayNote: string, order: number): MockAsk {
  const bank = issuerName(card);
  const must: MustAsk[] = [
    { id: "cycle", label: "Which billing cycle does the new date start with?", match: "which cycle|billing cycle|start|when does|take effect", answer: "The new due date starts with the billing cycle after your next statement." },
    { id: "next", label: "Is my next payment still due on the old date?", match: "next payment|old date|still due|this month", answer: `Yes. Your next payment is still due on the ${ordinal(card.due_day)}. The new date applies after that.` },
  ];
  const nodes: Record<string, CallNode> = {
    start: {
      say: `Hi, I'm calling about my ${card.name} card${card.last4 ? ` ending in ${card.last4}` : ""}. My payment is due on the ${ordinal(card.due_day)}. I'd like to move the due date to ${paydayNote}.`,
      hint: "Many issuers also let you change this online, under payment settings.",
      options: [
        { label: "They can move it", next: "granted" },
        { label: "Only some dates are available", next: "partial" },
        { label: "They said no", next: "refused_done" },
      ],
    },
    partial: {
      say: "Which dates are available? I'd like the closest one after my payday.",
      options: [{ label: "I picked a date", next: "granted" }],
    },
    granted: {
      say: "Thank you. Which billing cycle does the new date start with? Is my next payment still due on the old date?",
      hint: "Keep autopay on. Log the new date so the plan uses it.",
      options: [],
      outcome: "granted",
    },
    refused_done: {
      say: "Thanks for checking. Can I change it online instead, or ask again later?",
      options: [],
      outcome: "refused",
    },
  };
  return {
    id: `ask_${card.id}_due`,
    type: "due_date_move",
    card_id: card.id,
    card_name: card.name,
    issuer: card.issuer,
    title: `${bank} · move the due date`,
    subtitle: `To ${paydayNote}`,
    value_cents: null,
    value_label: dateShort(nextDue),
    status: "queued",
    order,
    result: null,
    why: `${card.name} is due on the ${ordinal(card.due_day)}, which lands a week after your payday. A due date right after payday keeps this minimum inside one paycheck. It does not change your interest. It lowers the chance of a late fee.`,
    evidence: {
      text: "Issuers must keep your due date on the same day every month, and most let you pick that day.",
      source: "Regulation Z, 12 CFR 1026.7(b)(11); issuer account settings.",
      caveat: `There is no data on how often issuers agree. ${bank} may offer only some dates, and the change can take a billing cycle.`,
    },
    script: {
      opening: `Hi, I'm calling about my ${card.name} card${card.last4 ? ` ending in ${card.last4}` : ""}.`,
      lines: [
        { label: "The ask", text: `My payment is due on the ${ordinal(card.due_day)}. I'd like to move the due date to ${paydayNote}.` },
        { label: "If yes", text: "Which billing cycle does the new date start with?" },
        { label: "Check", text: "Is my next payment still due on the old date?" },
      ],
      must_ask: must.map((m) => m.label),
      if_refused: "Can I change it online instead, or is there a date I could ask again?",
    },
    written: `Hello,\n\nThe payment on my ${card.name} card${card.last4 ? ` (ending in ${card.last4})` : ""} is due on the ${ordinal(card.due_day)} of each month. I would like to move the due date to ${paydayNote}.\n\nPlease confirm which billing cycle the new date starts with and whether my next payment is still due on the old date.\n\nThank you.`,
    call_card: { start: "start", nodes },
    playbook: { count: 1, entries: [{ channel: "message", result: "granted", note: "Changed in account settings; took effect after one cycle.", month: "2026-10" }] },
    must,
    rep: {
      scenario: "The rep can move the due date, starting with a later billing cycle.",
      opening: `Thank you for calling ${bank}, this is Jordan. How can I help you today?`,
      beats: [
        "Sure, I can help with that. Which day of the month would you like?",
        "I can set that. I'll put the change through now. Anything you'd like to check first?",
      ],
      closing: "The new due date is saved. You'll get a confirmation by mail. Thanks for calling.",
    },
  };
}

export function buildAsk(
  type: AskType,
  card: AskCard,
  order: number,
  ctx: { late_fee_cents?: number; next_due?: string; payday_note?: string } = {},
): MockAsk {
  if (type === "late_fee_refund") return lateFee(card, ctx.late_fee_cents ?? 3900, order);
  if (type === "due_date_move") return dueDateMove(card, ctx.next_due ?? "", ctx.payday_note ?? "two days after payday", order);
  return aprReduction(card, order);
}
