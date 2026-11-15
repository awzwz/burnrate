import { useId, useRef, useState } from "react";
import type { DragEvent, FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, errorMessage } from "../../api/client";
import type { Statement } from "../../api/types";
import { ErrorBox, Loading } from "../../components/Bits";
import { StatementInput, unitOf } from "../../components/StatementInputs";
import { dateShort } from "../../lib/format";
import { collectFields, STATEMENT_FIELDS } from "../../lib/statementFields";
import { useLoad } from "../../lib/useLoad";

export const STATUS_TEXT: Record<Statement["status"], string> = {
  extracting: "Reading",
  needs_confirmation: "Needs your check",
  reconciled: "Checked",
  failed: "Could not read",
};
export function StatusPill({ status, testId = "statement-status" }: { status: Statement["status"]; testId?: string }) {
  const cls = status === "reconciled" ? "ok" : status === "needs_confirmation" ? "warn" : status === "failed" ? "bad" : "";
  return (
    <span className={`pill ${cls}`} data-testid={testId} data-status={status}>
      {STATUS_TEXT[status]}
    </span>
  );
}

const MAX_BYTES = 10 * 1024 * 1024;
type Mode = "file" | "text" | "manual";

export default function Statements() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const list = useLoad(() => api.statements(), []);
  const cards = useLoad(() => api.cards(), []);
  const [cardId, setCardId] = useState(params.get("card") ?? "");
  const [mode, setMode] = useState<Mode>(params.get("mode") === "manual" ? "manual" : "file");
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const id = useId();

  const cardList = cards.data?.cards ?? [];
  const selected = cardId || cardList[0]?.id || "";

  const pick = (f: File | undefined | null) => {
    if (!f) return;
    if (!/\.(pdf|png|jpe?g)$/i.test(f.name) && !/pdf|png|jpe?g/i.test(f.type)) return setError("Choose a PDF, PNG or JPG.");
    if (f.size > MAX_BYTES) return setError("That file is larger than 10 MB. A photo of the summary page is enough.");
    setError(null);
    setFile(f);
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    pick(e.dataTransfer.files?.[0]);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return setError("Choose the card this statement belongs to.");
    setError(null);
    let call: () => Promise<Statement>;
    if (mode === "file") {
      if (!file) return setError("Choose the statement file first.");
      call = () => api.uploadStatement(selected, { file });
    } else if (mode === "text") {
      if (!text.trim()) return setError("Paste the statement text first.");
      call = () => api.uploadStatement(selected, { text });
    } else {
      const { fields, bad } = collectFields(STATEMENT_FIELDS, values);
      if (bad) return setError(`"${bad.label}" is not in the right format.`);
      if (Object.keys(fields).length === 0) return setError("Enter at least the new balance or the interest charged.");
      call = () => api.manualStatement(selected, fields);
    }
    setBusy(true);
    try {
      const st = await call();
      api.track("statement_added", { mode });
      navigate(`/app/statements/${st.id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  const core = STATEMENT_FIELDS.filter((f) => f.core);
  const rest = STATEMENT_FIELDS.filter((f) => !f.core);

  return (
    <div className="narrow">
      <div className="pd-h">
        <div>
          <h1>Statements</h1>
          <p className="lead">Each month we put our forecast next to your bank's own number and explain the difference.</p>
        </div>
      </div>

      <form className="tile" style={{ marginTop: 18 }} onSubmit={submit} noValidate aria-label="Add a statement">
        <h3>Add a statement</h3>
        <div className="form" style={{ marginTop: 12 }}>
          <div className="fld">
            <label htmlFor={`${id}-card`}>Card</label>
            <select id={`${id}-card`} className="inp" value={selected} onChange={(e) => setCardId(e.target.value)} data-testid="statement-card">
              {cardList.length === 0 && <option value="">{cards.loading ? "Loading cards" : "No cards yet"}</option>}
              {cardList.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>

          <fieldset className="opts" style={{ margin: 0, gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
            <legend className="sr">How to add it</legend>
            {(
              [
                ["file", "Upload a file"],
                ["text", "Paste the text"],
                ["manual", "Enter the numbers"],
              ] as [Mode, string][]
            ).map(([value, label]) => (
              <label className="opt" key={value} style={{ minHeight: 48, padding: "10px 12px", fontSize: 14, lineHeight: "20px", textAlign: "center", gridTemplateColumns: "1fr" }}>
                <input type="radio" name="mode" value={value} checked={mode === value} onChange={() => { setMode(value); setError(null); }} data-testid={`statement-mode-${value}`} />
                <span>{label}</span>
              </label>
            ))}
          </fieldset>

          {mode === "file" && (
            <label className={`drop ${over ? "over" : ""}`} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop}>
              <input ref={fileInput} className="sr" type="file" accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg" onChange={(e) => pick(e.target.files?.[0])} data-testid="statement-file" />
              {file ? (
                <span>
                  <span style={{ color: "var(--text)", fontWeight: 600 }}>{file.name}</span> · <b>choose another</b>
                </span>
              ) : (
                <span>
                  Drop the statement here or <b>choose a file</b>
                </span>
              )}
              <span style={{ fontSize: 13 }}>PDF, PNG or JPG, up to 10 MB. The summary page is enough.</span>
            </label>
          )}

          {mode === "text" && (
            <div className="fld">
              <label htmlFor={`${id}-text`}>Statement text</label>
              <textarea id={`${id}-text`} className="inp" value={text} onChange={(e) => setText(e.target.value)} placeholder="Select the summary and the interest table in your PDF, copy, and paste here." data-testid="statement-text" />
            </div>
          )}

          {mode === "manual" && (
            <>
              <div className="two">
                {core.map((def) => (
                  <div className="fld" key={def.key}>
                    <label htmlFor={`${id}-${def.key}`}>
                      {def.label}
                      {unitOf(def)}
                    </label>
                    <StatementInput def={def} id={`${id}-${def.key}`} value={values[def.key] ?? ""} onChange={(v) => setValues((s) => ({ ...s, [def.key]: v }))} />
                  </div>
                ))}
              </div>
              <details className="where" style={{ marginTop: 0 }}>
                <summary>More numbers from the statement (optional)</summary>
                <div className="two" style={{ marginTop: 12 }}>
                  {rest.map((def) => (
                    <div className="fld" key={def.key}>
                      <label htmlFor={`${id}-${def.key}`}>
                        {def.label}
                        {unitOf(def)}
                      </label>
                      <StatementInput def={def} id={`${id}-${def.key}`} value={values[def.key] ?? ""} onChange={(v) => setValues((s) => ({ ...s, [def.key]: v }))} />
                    </div>
                  ))}
                </div>
              </details>
            </>
          )}
        </div>
        {error && (
          <p className="err" role="alert">
            {error}
          </p>
        )}
        <div className="row-btns" style={{ marginTop: 16 }}>
          <button className="btn" type="submit" disabled={busy || cardList.length === 0} data-testid="statement-upload">
            {busy ? "Sending" : mode === "manual" ? "Check these numbers" : "Read this statement"}
          </button>
        </div>
        <p className="fine">We use the statement only to read the numbers above. Card numbers are not needed and not kept.</p>
      </form>

      <div className="sec">
        <h2>Your statements</h2>
        <span>newest first</span>
      </div>
      {list.data ? (
        list.data.statements.length ? (
          <ul className="list num">
            {list.data.statements.map((s) => (
              <li key={s.id} className="link">
                <Link className="rowlink" to={`/app/statements/${s.id}`} data-testid="statement-item">
                  <div className="nm">
                    {s.card_name}
                    <small>
                      {s.close_date ? `Closed ${dateShort(s.close_date)}` : "Closing date not read yet"}
                      {s.source === "manual" ? " · entered by you" : s.source === "demo" ? " · sample statement" : ""}
                    </small>
                  </div>
                  <div className="v">
                    <StatusPill status={s.status} testId="statement-item-status" />
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="note">No statements yet. Add the latest one for any card to see the first check.</p>
        )
      ) : list.loading ? (
        <Loading />
      ) : (
        <ErrorBox message={list.error ?? "We couldn't load your statements."} onRetry={list.reload} />
      )}
    </div>
  );
}
