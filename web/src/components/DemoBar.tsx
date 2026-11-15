import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { ClockAction, InboxMessage } from "../api/types";
import { dateLong, dateTime } from "../lib/format";
import { useSession } from "../state/session";

const ACTIONS: { action: ClockAction; label: string; id: string }[] = [
  { action: "next_payday", label: "Next payday", id: "demo-next-payday" },
  { action: "next_statement", label: "Next statement", id: "demo-next-statement" },
  { action: "plus_day", label: "+1 day", id: "demo-plus-day" },
  { action: "reset", label: "Restart", id: "demo-reset" },
];

/** The slim bar on every product screen of a demo session: what the data is, and the clock controls. */
export default function DemoBar() {
  const { me, setMe, bump } = useSession();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<InboxMessage[] | null>(null);
  const [inboxError, setInboxError] = useState<string | null>(null);
  const [seen, setSeen] = useState<number | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLButtonElement>(null);

  const loadInbox = useCallback(() => {
    api
      .inbox()
      .then((r) => {
        setMessages(r.messages);
        setInboxError(null);
        setSeen((s) => (s === null ? r.messages.length : s));
      })
      .catch((e) => setInboxError(errorMessage(e)));
  }, []);

  useEffect(() => {
    loadInbox();
  }, [loadInbox]);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!me?.demo) return null;
  const persona = me.demo.persona.charAt(0).toUpperCase() + me.demo.persona.slice(1);
  const fresh = messages && seen !== null ? Math.max(0, messages.length - seen) : 0;

  const jump = async (action: ClockAction) => {
    setBusy(true);
    try {
      const r = await api.demoClock(action);
      setMe(r.me);
      setNote(r.note);
      if (action === "reset") setSeen(null);
      bump();
      loadInbox(); // the inbox refreshes after every clock jump
    } catch (e) {
      setNote(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  function close() {
    setOpen(false);
    setSeen(messages ? messages.length : null);
    opener.current?.focus();
  }

  return (
    <>
      <div className="demo" data-testid="demo-bar" role="region" aria-label="Demo controls">
        <span className="num">
          <b>Demo</b> · {persona}'s numbers · {dateLong(me.demo.today)}
        </span>
        <span className="ctl">
          {ACTIONS.map((a) => (
            <button key={a.action} type="button" onClick={() => jump(a.action)} disabled={busy} data-testid={a.id}>
              {a.label}
              {a.action.startsWith("next") ? " →" : ""}
            </button>
          ))}
          <button
            type="button"
            ref={opener}
            onClick={() => {
              setOpen(true);
              loadInbox();
            }}
            aria-haspopup="dialog"
            data-testid="demo-inbox"
          >
            Inbox{fresh > 0 ? ` (${fresh} new)` : ""}
          </button>
        </span>
      </div>
      {note && (
        <div className="demo-note" role="status" data-testid="demo-note">
          <span>{note}</span>
          <button type="button" onClick={() => setNote(null)}>
            Hide
          </button>
        </div>
      )}
      {open && (
        <>
          <div className="scrim" onClick={close} />
          <div className="drawer" role="dialog" aria-modal="true" aria-labelledby="inbox-title">
            <header>
              <div>
                <h2 id="inbox-title">Inbox</h2>
                <p>Texts and emails the product would have sent. Demo only: nothing leaves this page.</p>
              </div>
              <button className="x" type="button" onClick={close} ref={closeRef} aria-label="Close inbox">
                ×
              </button>
            </header>
            <div className="msgs">
              {inboxError && <p className="err">{inboxError}</p>}
              {messages && messages.length === 0 && <p className="note">Nothing has been sent yet.</p>}
              {messages?.map((m) => (
                <article className="imsg" key={m.id} data-testid="inbox-message">
                  <div className="meta num">
                    <b>{m.channel === "sms" ? "Text message" : "Email"}</b>
                    <span>{dateTime(m.sent_at)}</span>
                  </div>
                  {m.subject && <h3>{m.subject}</h3>}
                  <p>{m.body}</p>
                  <div className="meta" style={{ marginTop: 6 }}>
                    <span>To {m.to}</span>
                  </div>
                  {m.link &&
                    (m.link.startsWith("/") ? (
                      <Link to={m.link} onClick={close}>
                        Open the link in this message
                      </Link>
                    ) : (
                      <a href={m.link} target="_blank" rel="noreferrer">
                        Open the link in this message
                      </a>
                    ))}
                </article>
              ))}
            </div>
          </div>
        </>
      )}
    </>
  );
}
