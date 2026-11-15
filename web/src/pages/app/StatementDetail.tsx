import { useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { api, errorMessage } from "../../api/client";
import type { Statement } from "../../api/types";
import { ErrorBox, Loading } from "../../components/Bits";
import { Chevron } from "../../components/Icons";
import { useShell } from "../../components/ProductShell";
import { StatementInput } from "../../components/StatementInputs";
import { dateShort, usd } from "../../lib/format";
import { collectFields, fieldToInput, showField, STATEMENT_FIELDS } from "../../lib/statementFields";
import { useLoad } from "../../lib/useLoad";
import { StatusPill } from "./Statements";

function Reconciliation({ st }: { st: Statement }) {
  const r = st.reconciliation;
  if (!r) return null;
  return (
    <section aria-label="Predicted vs actual" data-testid="statement-reconcile">
      <div className="sec">
        <h2>Predicted vs actual</h2>
        <span>{r.within_tolerance ? "within rounding" : "explained below"}</span>
      </div>
      <div className="pva num">
        <div className="pva-r">
          <span>Our forecast of the interest</span>
          <b>{usd(r.predicted_interest_cents)}</b>
        </div>
        <div className="pva-r">
          <span>“Interest Charged” on the statement</span>
          <b>{usd(r.actual_interest_cents)}</b>
        </div>
        <div className={`pva-r diff ${r.within_tolerance ? "" : "off"}`}>
          <span>Difference</span>
          <b>{usd(r.diff_cents)}</b>
        </div>
        {r.parts.map((p, i) => (
          <div className="pva-r part" key={i}>
            <span>{p.label}</span>
            <b>{usd(p.cents)}</b>
          </div>
        ))}
        <p>{r.summary}</p>
      </div>
    </section>
  );
}

function Fields({ st, onConfirmed }: { st: Statement; onConfirmed: (s: Statement) => void }) {
  const needs = st.status === "needs_confirmation";
  const [editAll, setEditAll] = useState(false);
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(STATEMENT_FIELDS.map((d) => [d.key, fieldToInput(d, st.fields)])));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  const highlighted = new Set(st.confirm_fields);
  const editable = (key: string) => needs && (editAll || highlighted.has(key));
  const shown = STATEMENT_FIELDS.filter((d) => needs || (st.fields[d.key] !== null && st.fields[d.key] !== undefined));

  const confirm = async (e: FormEvent) => {
    e.preventDefault();
    const defs = STATEMENT_FIELDS.filter((d) => editable(d.key));
    const { fields, bad } = collectFields(defs, values);
    if (bad) return setError(`"${bad.label}" is not in the right format.`);
    setBusy(true);
    setError(null);
    try {
      onConfirmed(await api.confirmStatement(st.id, { ...st.fields, ...fields }));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={confirm} noValidate>
      <div className="sec">
        <h2>What we read</h2>
        {needs && <span>{highlighted.size ? "check the marked lines" : "check the numbers"}</span>}
      </div>
      <dl className="kv" data-testid="statement-fields">
        {shown.map((d) => (
          <div key={d.key} style={{ display: "contents" }}>
            <dt className={needs && highlighted.has(d.key) ? "hl" : ""} data-highlight={needs && highlighted.has(d.key) ? "true" : undefined}>
              <label htmlFor={editable(d.key) ? `${id}-${d.key}` : undefined}>{d.label}</label>
            </dt>
            <dd>{editable(d.key) ? <StatementInput def={d} id={`${id}-${d.key}`} value={values[d.key] ?? ""} onChange={(v) => setValues((s) => ({ ...s, [d.key]: v }))} compact /> : showField(d, st.fields)}</dd>
          </div>
        ))}
      </dl>
      {needs && (
        <>
          {error && (
            <p className="err" role="alert">
              {error}
            </p>
          )}
          <div className="row-btns" style={{ marginTop: 16 }}>
            <button className="btn" type="submit" disabled={busy} data-testid="statement-confirm">
              {busy ? "Checking" : "These numbers are right"}
            </button>
            {!editAll && (
              <button className="textlink quiet" type="button" onClick={() => setEditAll(true)} style={{ fontSize: 14 }}>
                Correct another number
              </button>
            )}
          </div>
        </>
      )}
    </form>
  );
}

export default function StatementDetail() {
  const { id = "" } = useParams();
  const shell = useShell();
  const { data: st, error, loading, reload, setData } = useLoad(() => api.statement(id), [id]);

  // Poll every second while the statement is being read.
  useEffect(() => {
    if (!st || st.status !== "extracting") return;
    const t = window.setTimeout(() => {
      api
        .statement(id)
        .then((next) => {
          setData(next);
          if (next.status === "reconciled") shell.refresh();
        })
        .catch(() => reload());
    }, 1000);
    return () => window.clearTimeout(t);
  }, [st, id, setData, reload, shell]);

  const back = (
    <Link className="crumb" to="/app/statements">
      <Chevron /> Statements
    </Link>
  );
  if (!st) {
    return (
      <>
        {back}
        {loading ? <Loading /> : <ErrorBox message={error ?? "We couldn't load this statement."} onRetry={reload} />}
      </>
    );
  }

  const failed = st.checks.filter((c) => c.ok === false).length;

  return (
    <div className="narrow">
      {back}
      <div className="pd-h">
        <div>
          <h1>{st.card_name}</h1>
          <p className="lead">
            {st.close_date ? `Statement closed ${dateShort(st.close_date)}` : "Statement"}
            {st.due_date ? ` · due ${dateShort(st.due_date)}` : ""}
            {st.source === "demo" ? " · sample statement" : st.source === "manual" ? " · entered by you" : ""}
          </p>
        </div>
        <StatusPill status={st.status} />
      </div>

      {st.status === "extracting" && (
        <div className="tile" style={{ marginTop: 18 }} role="status">
          <h3>Reading your statement</h3>
          <p>This usually takes a few seconds. The page updates by itself.</p>
          <Loading label="Reading" />
        </div>
      )}

      {st.status === "failed" && (
        <div className="tile" style={{ marginTop: 18 }} role="alert">
          <h3>We couldn't read this one</h3>
          <p>{st.error ?? "The file did not contain statement numbers we could use."}</p>
          <div className="row-btns" style={{ marginTop: 12 }}>
            <Link className="btn sm" to={`/app/statements?mode=manual&card=${encodeURIComponent(st.card_id)}`}>
              Enter the numbers yourself
            </Link>
            <Link className="ghost sm" to="/app/statements">
              Try another file
            </Link>
          </div>
        </div>
      )}

      {st.status === "needs_confirmation" && (
        <p className="note" style={{ marginTop: 18 }}>
          <b>Please check {st.confirm_fields.length === 1 ? "one line" : "the marked lines"} against your statement.</b> We could not be sure about{" "}
          {st.confirm_fields.length === 1 ? "it" : "them"}. Correct anything that is off, then confirm.
        </p>
      )}

      <Reconciliation st={st} />

      {st.status === "reconciled" && st.triggers.length > 0 && (
        <section aria-label="Suggested next asks">
          <div className="sec">
            <h2>What this statement opens up</h2>
            <Link to="/app/asks">Open asks</Link>
          </div>
          <ul className="list" data-testid="statement-triggers">
            {st.triggers.map((t, i) => (
              <li key={i} className="link">
                <Link className="rowlink" to="/app/asks">
                  <div className="nm">
                    {t.title}
                    <small>{t.detail}</small>
                  </div>
                  <div className="v" aria-hidden="true">
                    <Chevron />
                  </div>
                </Link>
              </li>
            ))}
          </ul>
          <p className="fine">You make each call yourself; an issuer may say no.</p>
        </section>
      )}

      {(st.status === "needs_confirmation" || st.status === "reconciled") && (
        <>
          <Fields
            st={st}
            key={`${st.status}-${st.confirm_fields.join(",")}`}
            onConfirmed={(next) => {
              setData(next);
              shell.refresh();
            }}
          />
          {st.checks.length > 0 && (
            <section aria-label="Checks">
              <div className="sec">
                <h2>Checks</h2>
                <span>{failed ? `${failed} to look at` : "arithmetic on the statement's own numbers"}</span>
              </div>
              <ul className="checks" data-testid="statement-checks">
                {st.checks.map((c) => (
                  <li key={c.name}>
                    <i className={c.ok === false ? "no" : c.ok === null ? "na" : ""} aria-hidden="true">
                      {c.ok === false ? "!" : c.ok === null ? "?" : "✓"}
                    </i>
                    <div>
                      <b>
                        {c.name}
                        <span className="sr">{c.ok === false ? " (does not match)" : c.ok === null ? " (not checked)" : " (passes)"}</span>
                      </b>
                      <span>{c.detail}</span>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}
