import type { StatementFieldName, StatementFields } from "../api/types";
import { bpsToInput, centsToInput, dateFull, parseAprToBps, parseMoneyToCents, pct, usd } from "./format";

export type FieldKind = "money" | "rate" | "int" | "date";
export interface FieldDef {
  key: StatementFieldName;
  label: string;
  kind: FieldKind;
  /** Shown in the short "enter the numbers yourself" form. */
  core?: boolean;
}

// Labels follow the wording printed on US card statements.
export const STATEMENT_FIELDS: FieldDef[] = [
  { key: "close_date", label: "Closing date", kind: "date" },
  { key: "due_date", label: "Payment due date", kind: "date" },
  { key: "previous_balance_cents", label: "Previous balance", kind: "money" },
  { key: "payments_credits_cents", label: "Payments and credits", kind: "money" },
  { key: "purchases_cents", label: "Purchases", kind: "money" },
  { key: "fees_cents", label: "Fees charged", kind: "money" },
  { key: "interest_cents", label: "Interest charged", kind: "money", core: true },
  { key: "new_balance_cents", label: "New balance", kind: "money", core: true },
  { key: "min_payment_cents", label: "Minimum payment due", kind: "money", core: true },
  { key: "apr_bps", label: "Purchase APR", kind: "rate", core: true },
  { key: "balance_subject_to_interest_cents", label: "Balance subject to interest rate", kind: "money" },
  { key: "days_in_cycle", label: "Days in billing cycle", kind: "int" },
  { key: "late_fee_cents", label: "Late fee", kind: "money", core: true },
  { key: "interest_ytd_cents", label: "Total interest this year", kind: "money" },
];

export function showField(def: FieldDef, fields: StatementFields): string {
  const v = fields[def.key];
  if (v === null || v === undefined) return "—";
  if (def.kind === "money") return usd(v as number);
  if (def.kind === "rate") return pct(v as number);
  if (def.kind === "date") return dateFull(v as string);
  return String(v);
}

export function fieldToInput(def: FieldDef, fields: StatementFields): string {
  const v = fields[def.key];
  if (v === null || v === undefined) return "";
  if (def.kind === "money") return centsToInput(v as number);
  if (def.kind === "rate") return bpsToInput(v as number);
  return String(v);
}

/** Typed text -> the API value. `undefined` means empty; `null` means "not in the right format". */
export function parseField(def: FieldDef, text: string): number | string | null | undefined {
  const t = text.trim();
  if (!t) return undefined;
  if (def.kind === "money") return parseMoneyToCents(t);
  if (def.kind === "rate") return parseAprToBps(t);
  if (def.kind === "int") return /^\d{1,3}$/.test(t) ? Number(t) : null;
  return /^\d{4}-\d{2}-\d{2}$/.test(t) ? t : null;
}

/** Turns a map of typed values into StatementFields, or names the first field that is not in the right format. */
export function collectFields(defs: FieldDef[], values: Record<string, string>): { fields: StatementFields; bad: FieldDef | null } {
  const fields: Record<string, number | string> = {};
  for (const def of defs) {
    const parsed = parseField(def, values[def.key] ?? "");
    if (parsed === null) return { fields: {}, bad: def };
    if (parsed !== undefined) fields[def.key] = parsed;
  }
  return { fields: fields as StatementFields, bad: null };
}
