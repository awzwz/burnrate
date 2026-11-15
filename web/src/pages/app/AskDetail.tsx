import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, errorMessage } from "../../api/client";
import type { AskDetail, Outcome, OutcomeResult, Rehearsal, Today } from "../../api/types";
import { ErrorBox, Loading } from "../../components/Bits";
import { Check, Chevron } from "../../components/Icons";
import { useShell } from "../../components/ProductShell";
import { centsToInput, monthYear, parseAprToBps, parseMoneyToCents, usd, usdPerDay } from "../../lib/format";
import { useLoad } from "../../lib/useLoad";
import { AskStatus, RESULT_TEXT } from "./Asks";

const TABS = [
  { id: "why", label: "Why" },
  { id: "script", label: "Script" },
  { id: "written", label: "Written version" },
  { id: "rehearse", label: "Rehearse", testId: "ask-rehearse" },
  { id: "call", label: "Call card", testId: "ask-callcard" },
  { id: "outcome", label: "Log the outcome", testId: "ask-outcome" },
] as const;
type TabId = (typeof TABS)[number]["id"];

const CHANNEL: Record<string, string> = { phone: "Phone", chat: "Chat", message: "Secure message" };

// ---------- Why ----------
function Why({ ask, onSkipped }: { ask: AskDetail; onSkipped: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const skip = async () => {
    setBusy(true);
    try {
      await api.askSkip(ask.id);
      onSkipped();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="pane">
      <p>{ask.why}</p>
      <div className="stake num">
        <span className="k">{ask.value_cents !== null ? "At stake" : ask.type === "apr_reduction" ? "Your rate today" : "Today"}</span>
        <span className="v">{ask.value_cents !== null ? usd(ask.value_cents) : ask.value_label}</span>
      </div>
      <div className="evid">
        <div className="lab12">What is known</div>
        <p style={{ marginTop: 4 }}>{ask.evidence.text}</p>
        <p className="src">Source: {ask.evidence.source}</p>
        <p className="cav">{ask.evidence.caveat}</p>
      </div>
      {ask.playbook.count > 0 && (
        <>
          <div className="sec">
            <h2>What others reported</h2>
            <span className="num">{ask.playbook.count} {ask.playbook.count === 1 ? "report" : "reports"}</span>
          </div>
          <ul className="list">
            {ask.playbook.entries.map((e, i) => (
              <li key={i}>
                <div className="nm">
                  {RESULT_TEXT[e.result] ?? e.result}
                  <small>
                    {CHANNEL[e.channel] ?? e.channel} · {monthYear(e.month)} · {e.note}
                  </small>
                </div>
              </li>
            ))}
          </ul>
          <p className="fine">Reports from other users, as they logged them. Not a forecast for your account.</p>
        </>
      )}
      {ask.status === "queued" && (
        <p style={{ marginTop: 24, fontSize: 14 }}>
          <button className="textlink quiet" type="button" onClick={skip} disabled={busy} data-testid="ask-skip">
            Skip this ask
          </button>
        </p>
      )}
      {error && <p className="err">{error}</p>}
    </div>
  );
}

// ---------- Script ----------
function Script({ ask }: { ask: AskDetail }) {
  return (
    <div className="pane">
      <ol className="script">
        <li>
          <div className="lab12">Opening</div>
          <p>{ask.script.opening}</p>
        </li>
        {ask.script.lines.map((l, i) => (
          <li key={i}>
            <div className="lab12">{l.label}</div>
            <p>{l.text}</p>
          </li>
        ))}
      </ol>
      {ask.script.must_ask.length > 0 && (
        <>
          <div className="sec">
            <h2>Ask these before you agree</h2>
          </div>
          <ul className="must">
            {ask.script.must_ask.map((q) => (
              <li key={q}>
                <i aria-hidden="true" />
                {q}
              </li>
            ))}
          </ul>
        </>
      )}
      <div className="sec">
        <h2>If they say no</h2>
      </div>
      <ol className="script">
        <li>
          <p style={{ marginTop: 0 }}>{ask.script.if_refused}</p>
        </li>
      </ol>
      <p className="fine">Call the number on the back of your card. The numbers in this script are yours, from your cards and statements.</p>
    </div>
  );
}

// ---------- Written ----------
function Written({ ask }: { ask: AskDetail }) {
  const [copied, setCopied] = useState<"no" | "yes" | "failed">("no");
  const pre = useRef<HTMLPreElement>(null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(ask.written);
      setCopied("yes");
    } catch {
      // No clipboard permission: select the text so it can be copied by hand.
      const sel = window.getSelection();
      if (pre.current && sel) {
        const range = document.createRange();
        range.selectNodeContents(pre.current);
        sel.removeAllRanges();
        sel.addRange(range);
      }
      setCopied("failed");
    }
  };
  return (
    <div className="pane">
      <p style={{ fontSize: 15, lineHeight: "22px", color: "var(--text-2)" }}>For chat or your bank's secure messages. You send it yourself from your own account.</p>
      <pre className="written" ref={pre} style={{ marginTop: 12 }} data-testid="ask-written">
        {ask.written}
      </pre>
      <div className="row-btns" style={{ marginTop: 12 }}>
        <button className="btn" type="button" onClick={copy} data-testid="ask-copy">
          Copy text
        </button>
        <span role="status" style={{ fontSize: 14, color: "var(--text-2)" }}>
          {copied === "yes" ? "Copied." : copied === "failed" ? "The text is selected. Copy it with your keyboard or the share menu." : ""}
        </span>
      </div>
    </div>
  );
}

// ---------- Rehearse ----------
function Rehearse({ ask, onOpenCall }: { ask: AskDetail; onOpenCall: () => void }) {
  const key = `br-rehearsal-${ask.id}`;
  const [rh, setRh] = useState<Rehearsal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [streaming, setStreaming] = useState<string | null>(null);
  const [pendingLine, setPendingLine] = useState<string | null>(null);
  const started = useRef(false);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const id = useId();

  const begin = async (fresh: boolean) => {
    setError(null);
    try {
      let existing: string | null = null;
      try {
        existing = fresh ? null : sessionStorage.getItem(key);
      } catch {
        existing = null;
      }
      let r: Rehearsal | null = null;
      if (existing) r = await api.rehearsal(existing).catch(() => null);
      if (!r) r = await api.rehearsalStart(ask.id);
      try {
        sessionStorage.setItem(key, r.id);
      } catch {
        /* resuming after a reload just won't work in private mode */
      }
      setRh(r);
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void begin(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (typeof end.current?.scrollIntoView === "function" && (streaming !== null || pendingLine !== null)) {
      end.current.scrollIntoView({ block: "nearest" });
    }
  }, [streaming, pendingLine, rh]);

  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    const line = text.trim();
    if (!rh || !line || streaming !== null) return;
    setText("");
    setError(null);
    setPendingLine(line);
    setStreaming("");
    try {
      const next = await api.rehearsalTurn(rh.id, line, (piece) => setStreaming((s) => (s ?? "") + piece));
      setRh(next);
    } catch (err) {
      setError(errorMessage(err));
      setText(line);
    } finally {
      setStreaming(null);
      setPendingLine(null);
      input.current?.focus();
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className="pane" style={{ maxWidth: 920 }}>
      <p className="note">
        <b>Practice with an AI playing a bank rep.</b> It does not know your bank's real policy. What it offers here is practice material, not a
        forecast of what your bank will say.
      </p>
      {!rh && !error && <Loading label="Setting up the practice call" />}
      {error && !rh && <ErrorBox message={error} onRetry={() => void begin(false)} />}
      {rh && (
        <div className="rh-grid" style={{ marginTop: 16 }}>
          <div>
            <p className="fine" style={{ marginTop: 0 }}>
              Scenario: {rh.scenario}
            </p>
            <div className="chat" style={{ marginTop: 12 }} role="log" aria-live="polite" aria-label="Practice call">
              {rh.messages.map((m, i) => (
                <div className={`msg ${m.role}`} key={i} data-testid="rehearsal-message" data-role={m.role}>
                  <span className="who">{m.role === "rep" ? "Rep (AI)" : "You"}</span>
                  {m.text}
                </div>
              ))}
              {pendingLine !== null && (
                <div className="msg you" data-testid="rehearsal-message" data-role="you">
                  <span className="who">You</span>
                  {pendingLine}
                </div>
              )}
              {streaming !== null && (
                <div className="msg rep" data-testid="rehearsal-message" data-role="rep" data-streaming="true">
                  <span className="who">Rep (AI)</span>
                  {streaming || "…"}
                </div>
              )}
              <div ref={end} />
            </div>
            {rh.finished ? (
              <div className="result-box" data-testid="rehearsal-finished">
                <div className="lab12">Practice call ended</div>
                <p>
                  {rh.checklist.every((c) => c.done)
                    ? "You asked every question on the list."
                    : "Some questions on the list were not asked. Try once more, or keep the list open during the real call."}
                </p>
                <div className="row-btns" style={{ marginTop: 12 }}>
                  <button className="ghost sm" type="button" onClick={() => void begin(true)} data-testid="rehearsal-restart">
                    Practice again
                  </button>
                  <button className="btn sm" type="button" onClick={onOpenCall}>
                    Open the call card
                  </button>
                </div>
              </div>
            ) : (
              <form className="chat-in" onSubmit={send}>
                <label className="sr" htmlFor={`${id}-say`}>
                  What you would say
                </label>
                <textarea
                  id={`${id}-say`}
                  ref={input}
                  className="inp"
                  rows={1}
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={onKey}
                  placeholder="Type what you would say"
                  maxLength={1000}
                  data-testid="rehearsal-input"
                />
                <button className="btn" type="submit" disabled={streaming !== null || !text.trim()} data-testid="rehearsal-send">
                  Send
                </button>
              </form>
            )}
            {error && rh && (
              <p className="err" role="alert">
                {error}
              </p>
            )}
          </div>
          <aside aria-label="Questions to ask">
            <div className="lab12">Ask these during the call</div>
            <ul className="must" style={{ marginTop: 10 }} data-testid="rehearsal-checklist">
              {rh.checklist.map((c) => (
                <li key={c.id} className={c.done ? "done" : ""} data-done={c.done}>
                  <i aria-hidden="true">{c.done && <Check size={12} />}</i>
                  <span>
                    {c.label}
                    <span className="sr">{c.done ? " (asked)" : " (not asked yet)"}</span>
                  </span>
                </li>
              ))}
            </ul>
            {!rh.finished && rh.messages.length > 1 && (
              <p style={{ marginTop: 16, fontSize: 14 }}>
                <button className="textlink quiet" type="button" onClick={() => void begin(true)}>
                  Start over
                </button>
              </p>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

// ---------- Call card ----------
function CallCard({ ask, onLog }: { ask: AskDetail; onLog: (result: OutcomeResult | null) => void }) {
  const [path, setPath] = useState<string[]>([ask.call_card.start]);
  const at = path[path.length - 1];
  const node = ask.call_card.nodes[at];
  const heading = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (path.length > 1) heading.current?.focus();
  }, [path]);
  if (!node) return <ErrorBox message="This call card has no steps yet." />;
  const outcome = node.outcome && node.outcome in RESULT_TEXT ? (node.outcome as OutcomeResult) : null;
  const terminal = node.options.length === 0;
  return (
    <div className="pane" data-testid="callcard" data-node={at}>
      <div className="say" tabIndex={-1} ref={heading} style={{ outline: "none" }}>
        <div className="lab12">Say this</div>
        <p data-testid="callcard-say">{node.say}</p>
        {node.hint && <div className="hint">{node.hint}</div>}
      </div>
      {!terminal && (
        <>
          <div className="lab12" style={{ marginTop: 20, color: "var(--text-2)" }}>
            What did the rep say?
          </div>
          <div className="cc-opts">
            {node.options.map((o) => (
              <button className="ghost" type="button" key={`${o.next}-${o.label}`} onClick={() => setPath((p) => [...p, o.next])} data-testid="callcard-option">
                {o.label}
              </button>
            ))}
          </div>
        </>
      )}
      {terminal && (
        <div className="cc-opts">
          <button className="btn field-h wide" type="button" onClick={() => onLog(outcome)} data-testid="callcard-log">
            Log the outcome{outcome ? `: ${RESULT_TEXT[outcome].toLowerCase()}` : ""}
          </button>
        </div>
      )}
      <div className="cc-nav">
        {path.length > 1 && (
          <button className="textlink quiet" type="button" onClick={() => setPath((p) => p.slice(0, -1))} data-testid="callcard-back">
            Back one step
          </button>
        )}
        {path.length > 1 && (
          <button className="textlink quiet" type="button" onClick={() => setPath([ask.call_card.start])}>
            Start over
          </button>
        )}
      </div>
      <p className="fine">Nothing is recorded: this screen has no microphone access. Keep it open on the phone you are calling from.</p>
    </div>
  );
}

// ---------- Outcome ----------
function OutcomeForm({ ask, preset, onSaved }: { ask: AskDetail; preset: OutcomeResult | null; onSaved: (today: Today) => void }) {
  const [result, setResult] = useState<OutcomeResult | "">(preset ?? "");
  const [channel, setChannel] = useState<Outcome["channel"]>("phone");
  const [apr, setApr] = useState("");
  const [until, setUntil] = useState("");
  const [refund, setRefund] = useState(ask.value_cents !== null && ask.type !== "apr_reduction" ? centsToInput(ask.value_cents) : "");
  const [rep, setRep] = useState("");
  const [hardPull, setHardPull] = useState<"" | "yes" | "no" | "unknown">("");
  const [again, setAgain] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<Today | null>(null);
  const id = useId();

  useEffect(() => {
    if (preset) setResult(preset);
  }, [preset]);

  const good = result === "granted" || result === "partial" || result === "temporary";
  const isRate = ask.type === "apr_reduction" || ask.type === "hardship";
  const isFee = ask.type === "late_fee_refund" || ask.type === "annual_fee";

  if (saved) {
    return (
      <div className="pane">
        <div className="result-box" data-testid="ask-outcome-saved">
          <div className="lab12" style={{ color: "var(--ok)" }}>
            Logged
          </div>
          <div className="big num" data-testid="ask-outcome-burn">
            {usdPerDay(saved.burn.now)} a day
          </div>
          <p>
            That is what your cards cost you per day now.{" "}
            {good
              ? "The next statement should show the change; we will check it against what you logged."
              : "A no is a logged result. Your payday plan carries on as it was."}
          </p>
          <div className="row-btns" style={{ marginTop: 14 }}>
            <Link className="btn sm" to="/app">
              Back to Today
            </Link>
            <Link className="ghost sm" to="/app/asks">
              See the queue
            </Link>
          </div>
        </div>
      </div>
    );
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!result) return setError("Choose what happened.");
    const outcome: Outcome = { result, channel };
    if (good && isRate && apr.trim()) {
      const bps = parseAprToBps(apr);
      if (bps === null) return setError("Enter the new rate as a number, like 24.99.");
      outcome.new_apr_bps = bps;
    }
    if (result === "temporary" && until) outcome.until = until;
    if (good && isFee && refund.trim()) {
      const cents = parseMoneyToCents(refund);
      if (cents === null) return setError("Enter the refund as an amount, like 39.00.");
      outcome.refund_cents = cents;
    }
    if (rep.trim()) outcome.rep_name = rep.trim();
    if (hardPull) outcome.hard_pull = hardPull;
    if (result === "refused" && again) outcome.ask_again_on = again;
    if (note.trim()) outcome.note = note.trim();
    setBusy(true);
    setError(null);
    try {
      const r = await api.askOutcome(ask.id, outcome);
      api.track("ask_outcome", { type: ask.type, result });
      setSaved(r.today);
      onSaved(r.today);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="pane" onSubmit={submit} noValidate>
      <p style={{ fontSize: 15, lineHeight: "22px", color: "var(--text-2)" }}>
        Write down what the rep said while it is fresh. A no is worth logging too: it tells the plan when to ask again.
      </p>
      <div className="form">
        <div className="two">
          <div className="fld">
            <label htmlFor={`${id}-result`}>What happened?</label>
            <select id={`${id}-result`} className="inp" value={result} onChange={(e) => setResult(e.target.value as OutcomeResult | "")} data-testid="ask-outcome-result">
              <option value="">Choose</option>
              <option value="granted">They said yes</option>
              <option value="partial">They agreed to part of it</option>
              <option value="temporary">They made a temporary offer</option>
              <option value="refused">They said no</option>
              <option value="no_answer">No answer yet</option>
            </select>
          </div>
          <div className="fld">
            <label htmlFor={`${id}-channel`}>How did you ask?</label>
            <select id={`${id}-channel`} className="inp" value={channel} onChange={(e) => setChannel(e.target.value as Outcome["channel"])} data-testid="ask-outcome-channel">
              <option value="phone">Phone</option>
              <option value="chat">Chat</option>
              <option value="message">Secure message</option>
            </select>
          </div>
        </div>

        {good && isRate && (
          <div className="two">
            <div className="fld">
              <label htmlFor={`${id}-apr`}>New rate (APR)</label>
              <div className="money pct">
                <input id={`${id}-apr`} className="inp num" inputMode="decimal" value={apr} onChange={(e) => setApr(e.target.value)} placeholder="0.00" autoComplete="off" aria-describedby={`${id}-apr-hint`} data-testid="ask-outcome-apr" />
                <span>%</span>
              </div>
              <span className="hint" id={`${id}-apr-hint`}>
                Your rate today: {ask.value_label}
              </span>
            </div>
            {result === "temporary" && (
              <div className="fld">
                <label htmlFor={`${id}-until`}>Until what date?</label>
                <input id={`${id}-until`} className="inp" type="date" value={until} onChange={(e) => setUntil(e.target.value)} data-testid="ask-outcome-until" />
              </div>
            )}
          </div>
        )}
        {good && isFee && (
          <div className="fld" style={{ maxWidth: 280 }}>
            <label htmlFor={`${id}-refund`}>Amount refunded</label>
            <div className="money">
              <span>$</span>
              <input id={`${id}-refund`} className="inp num" inputMode="decimal" value={refund} onChange={(e) => setRefund(e.target.value)} placeholder="0.00" autoComplete="off" data-testid="ask-outcome-refund" />
            </div>
          </div>
        )}
        {result === "refused" && (
          <div className="fld" style={{ maxWidth: 280 }}>
            <label htmlFor={`${id}-again`}>When did they say to ask again? (optional)</label>
            <input id={`${id}-again`} className="inp" type="date" value={again} onChange={(e) => setAgain(e.target.value)} />
          </div>
        )}
        <div className="two">
          <div className="fld">
            <label htmlFor={`${id}-rep`}>Rep's name (optional)</label>
            <input id={`${id}-rep`} className="inp" value={rep} onChange={(e) => setRep(e.target.value)} maxLength={60} autoComplete="off" />
          </div>
          <div className="fld">
            <label htmlFor={`${id}-pull`}>Hard inquiry?</label>
            <select id={`${id}-pull`} className="inp" value={hardPull} onChange={(e) => setHardPull(e.target.value as typeof hardPull)}>
              <option value="">Didn't come up</option>
              <option value="no">They said no</option>
              <option value="yes">They said yes</option>
              <option value="unknown">They didn't know</option>
            </select>
          </div>
        </div>
        <div className="fld">
          <label htmlFor={`${id}-note`}>Notes (optional)</label>
          <textarea id={`${id}-note`} className="inp" value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} style={{ minHeight: 88 }} />
        </div>
      </div>
      {error && (
        <p className="err" role="alert">
          {error}
        </p>
      )}
      <div className="row-btns" style={{ marginTop: 16 }}>
        <button className="btn" type="submit" disabled={busy} data-testid="ask-outcome-save">
          {busy ? "Saving" : "Save the outcome"}
        </button>
      </div>
    </form>
  );
}

// ---------- page ----------
export default function AskDetailPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const shell = useShell();
  const { data: ask, error, loading, reload } = useLoad(() => api.ask(id), [id]);
  const [preset, setPreset] = useState<OutcomeResult | null>(null);
  const requested = params.get("tab");
  const tab: TabId = TABS.some((t) => t.id === requested) ? (requested as TabId) : "why";
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const ready = !!ask;

  const go = (next: TabId) => setParams(next === "why" ? {} : { tab: next }, { replace: true });

  // On a phone the tab row scrolls sideways: keep the selected tab in view.
  useEffect(() => {
    const el = tabRefs.current[tab];
    if (ready && el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest", inline: "center" });
  }, [tab, ready]);

  if (!ask) {
    return (
      <>
        <Link className="crumb" to="/app/asks">
          <Chevron /> Asks
        </Link>
        {loading ? <Loading /> : <ErrorBox message={error ?? "We couldn't load this ask."} onRetry={reload} />}
      </>
    );
  }

  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = TABS.findIndex((t) => t.id === tab);
    let j = -1;
    if (e.key === "ArrowRight") j = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") j = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = TABS.length - 1;
    if (j < 0) return;
    e.preventDefault();
    go(TABS[j].id);
    tabRefs.current[TABS[j].id]?.focus();
  };

  return (
    <>
      <Link className="crumb" to="/app/asks">
        <Chevron /> Asks
      </Link>
      <div className="pd-h">
        <div>
          <h1 data-testid="ask-title">{ask.title}</h1>
          <p className="lead">
            {ask.subtitle} <AskStatus ask={ask} />
          </p>
        </div>
        <span className="chip num">{ask.value_label}</span>
      </div>

      <div className="seg" role="tablist" aria-label="Sections of this ask">
        {TABS.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              tabRefs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => go(t.id)}
            onKeyDown={onTabKey}
            data-testid={"testId" in t ? t.testId : `ask-tab-${t.id}`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "why" && <Why ask={ask} onSkipped={reload} />}
        {tab === "script" && <Script ask={ask} />}
        {tab === "written" && <Written ask={ask} />}
        {tab === "rehearse" && <Rehearse ask={ask} onOpenCall={() => go("call")} key={ask.id} />}
        {tab === "call" && (
          <CallCard
            ask={ask}
            onLog={(r) => {
              setPreset(r);
              go("outcome");
            }}
          />
        )}
        {tab === "outcome" && (
          <OutcomeForm
            ask={ask}
            preset={preset}
            onSaved={(today) => {
              shell.setToday(today);
              reload();
            }}
          />
        )}
      </div>
      {tab === "why" && ask.status === "queued" && (
        <div className="row-btns" style={{ marginTop: 24 }}>
          <button className="btn" type="button" onClick={() => go("rehearse")}>
            Rehearse the call
          </button>
          <button className="ghost" type="button" onClick={() => go("script")}>
            Read the script
          </button>
        </div>
      )}
    </>
  );
}
