import type { FieldDef } from "../lib/statementFields";

/** One labelled input per statement field. Used by manual entry and by the confirm step. */
export function StatementInput({ def, id, value, onChange, compact = false }: { def: FieldDef; id: string; value: string; onChange: (v: string) => void; compact?: boolean }) {
  const cls = `inp num${compact ? " sm" : ""}`;
  if (def.kind === "date") return <input id={id} className={cls} type="date" value={value} onChange={(e) => onChange(e.target.value)} data-testid={`statement-field-${def.key}`} />;
  return (
    <input
      id={id}
      className={cls}
      inputMode={def.kind === "int" ? "numeric" : "decimal"}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={def.kind === "int" ? "30" : "0.00"}
      autoComplete="off"
      data-testid={`statement-field-${def.key}`}
    />
  );
}

export const unitOf = (def: FieldDef) => (def.kind === "money" ? " ($)" : def.kind === "rate" ? " (%)" : "");
