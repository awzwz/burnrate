import { useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, errorMessage } from "../../api/client";
import type { Meter, NotificationPrefs, Subscription } from "../../api/types";
import { ErrorBox, Loading } from "../../components/Bits";
import MeterBox from "../../components/MeterBox";
import { dateFull, periodText, tsToISODate, usd } from "../../lib/format";
import { useLoad } from "../../lib/useLoad";
import { useSession } from "../../state/session";

const STATUS: Record<Subscription["status"], string> = {
  active: "Active",
  canceled: "Cancelled",
  paused: "Paused",
  past_due: "Payment needed",
  refunded: "Refunded",
};

function PlanSection({ sub, today, onChange }: { sub: Subscription; today: string; onChange: () => Promise<unknown> }) {
  const [panel, setPanel] = useState<"none" | "cancel" | "pause" | "refund">("none");
  const [weeks, setWeeks] = useState("4");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const id = useId();
  const periodEnd = dateFull(tsToISODate(sub.current_period_end));
  const ending = sub.cancel_at_period_end || sub.status === "canceled";
  const live = sub.status === "active" && !ending;
  const refundable = !!sub.refundable_until && sub.refundable_until >= today && sub.status !== "refunded";

  const run = async (call: () => Promise<Subscription>, message: (s: Subscription) => string) => {
    setBusy(true);
    setError(null);
    try {
      const next = await call();
      setDone(message(next));
      setPanel("none");
      await onChange();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="tile" aria-label="Your plan">
      <div className="lab12">Your plan</div>
      <h3 style={{ marginTop: 4 }} data-testid="settings-plan">
        {sub.plan.name} · {sub.status === "active" && ending ? "Cancelled, active until the period ends" : STATUS[sub.status]}
      </h3>
      <dl className="kv num" style={{ marginTop: 8 }}>
        <dt>Paid so far</dt>
        <dd>{usd(sub.paid_total_cents)}</dd>
        {live && (
          <>
            <dt>Renews on</dt>
            <dd data-testid="settings-renewal">{periodEnd}</dd>
            <dt>Renewal price</dt>
            <dd>
              {usd(sub.renews_price_cents)}
              <small>every {periodText(sub.plan.renews_days)}, until you cancel</small>
            </dd>
          </>
        )}
        {ending && sub.status !== "refunded" && (
          <>
            <dt>Access until</dt>
            <dd data-testid="settings-renewal">{periodEnd}</dd>
            <dt>Next charge</dt>
            <dd>None</dd>
          </>
        )}
        {sub.status === "paused" && (
          <>
            <dt>Paused until</dt>
            <dd>{dateFull(sub.paused_until)}</dd>
            <dt>Charges while paused</dt>
            <dd>None</dd>
          </>
        )}
        {sub.status === "refunded" && (
          <>
            <dt>Refund</dt>
            <dd>Sent to your card</dd>
          </>
        )}
      </dl>

      {done && (
        <p className="err okline" role="status" data-testid="settings-done">
          {done}
        </p>
      )}
      {error && (
        <p className="err" role="alert">
          {error}
        </p>
      )}

      {panel === "none" && (
        <div className="row-btns">
          {live && (
            <>
              <button className="ghost plain" type="button" onClick={() => setPanel("cancel")} data-testid="settings-cancel">
                Cancel plan
              </button>
              <button className="ghost plain" type="button" onClick={() => setPanel("pause")} data-testid="settings-pause">
                Pause plan
              </button>
            </>
          )}
          {(ending || sub.status === "paused") && sub.status !== "refunded" && (
            <button
              className="btn"
              type="button"
              disabled={busy}
              onClick={() => run(() => api.resume(), (s) => `Your plan is active again. It renews on ${dateFull(tsToISODate(s.current_period_end))} at ${usd(s.renews_price_cents)}.`)}
              data-testid="settings-resume"
            >
              Resume plan
            </button>
          )}
          {refundable && (
            <button className="ghost plain" type="button" onClick={() => setPanel("refund")} data-testid="settings-refund">
              Get a refund
            </button>
          )}
        </div>
      )}

      {panel === "cancel" && (
        <div className="confirm" role="group" aria-label="Cancel plan">
          <p>
            Cancel the {sub.plan.name}? You keep access until {periodEnd} and nothing more is charged.
          </p>
          <div className="row-btns">
            <button
              className="ghost plain"
              type="button"
              disabled={busy}
              onClick={() => run(() => api.cancel(), (s) => `Cancelled. You keep access until ${dateFull(tsToISODate(s.current_period_end))} and will not be charged again.`)}
              data-testid="settings-cancel-confirm"
            >
              Yes, cancel
            </button>
            <button className="ghost plain" type="button" disabled={busy} onClick={() => setPanel("pause")} data-testid="settings-pause-instead">
              Pause instead
            </button>
            <button className="textlink quiet" type="button" onClick={() => setPanel("none")} style={{ fontSize: 14 }}>
              Keep my plan
            </button>
          </div>
        </div>
      )}

      {panel === "pause" && (
        <div className="confirm" role="group" aria-label="Pause plan">
          <p>Pause the plan. Nothing is charged while it is paused, and you can resume or cancel at any time.</p>
          <div className="fld" style={{ marginTop: 12, maxWidth: 240 }}>
            <label htmlFor={`${id}-weeks`}>Pause for</label>
            <select id={`${id}-weeks`} className="inp sm" value={weeks} onChange={(e) => setWeeks(e.target.value)} data-testid="settings-pause-weeks">
              {[1, 2, 3, 4, 5, 6, 7, 8].map((w) => (
                <option key={w} value={w}>
                  {w} {w === 1 ? "week" : "weeks"}
                </option>
              ))}
            </select>
          </div>
          <div className="row-btns">
            <button
              className="ghost plain"
              type="button"
              disabled={busy}
              onClick={() => run(() => api.pause(Number(weeks)), (s) => `Paused${s.paused_until ? ` until ${dateFull(s.paused_until)}` : ""}. Nothing is charged while the plan is paused.`)}
              data-testid="settings-pause-confirm"
            >
              Pause plan
            </button>
            <button className="textlink quiet" type="button" onClick={() => setPanel("none")} style={{ fontSize: 14 }}>
              Not now
            </button>
          </div>
        </div>
      )}

      {panel === "refund" && (
        <div className="confirm" role="group" aria-label="Refund">
          <p>
            Refund {usd(sub.paid_total_cents)} to your card and end the plan now? No conditions. The money-back window is open until{" "}
            {dateFull(sub.refundable_until)}.
          </p>
          <div className="row-btns">
            <button className="ghost plain" type="button" disabled={busy} onClick={() => run(() => api.refund(), () => "Refunded. The money goes back to the card you paid with.")} data-testid="settings-refund-confirm">
              Yes, refund me
            </button>
            <button className="textlink quiet" type="button" onClick={() => setPanel("none")} style={{ fontSize: 14 }}>
              Keep my plan
            </button>
          </div>
        </div>
      )}

      {!refundable && sub.refundable_until && sub.status !== "refunded" && (
        <p className="fine">The money-back window for this plan ended {dateFull(sub.refundable_until)}.</p>
      )}
      <p className="fine">
        <Link className="textlink quiet" to="/terms">
          Subscription terms
        </Link>
      </p>
    </section>
  );
}

function Notifications({ prefs, smsLive, onSaved }: { prefs: NotificationPrefs; smsLive: boolean; onSaved: (p: NotificationPrefs) => void }) {
  const [phone, setPhone] = useState(prefs.phone ?? "");
  const [sms, setSms] = useState(prefs.sms_opt_in);
  const [email, setEmail] = useState(prefs.email_opt_in);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const id = useId();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const digits = phone.replace(/\D/g, "");
    if (phone.trim() && digits.length < 10) return setMsg({ ok: false, text: "Enter a 10-digit US mobile number, or leave it empty." });
    if (sms && !phone.trim()) return setMsg({ ok: false, text: "Add a mobile number to get payday texts." });
    setBusy(true);
    setMsg(null);
    try {
      onSaved(await api.savePrefs({ phone: phone.trim() || null, sms_opt_in: sms, email_opt_in: email, timezone: prefs.timezone }));
      setMsg({ ok: true, text: "Saved." });
    } catch (err) {
      setMsg({ ok: false, text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!prefs.calendar_url) return;
    try {
      await navigator.clipboard.writeText(prefs.calendar_url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <section className="tile" aria-label="Reminders">
      <div className="lab12">Reminders</div>
      <form onSubmit={submit} noValidate>
        <div className="form" style={{ marginTop: 8 }}>
          <div className="fld">
            <label htmlFor={`${id}-phone`}>Mobile number</label>
            <input id={`${id}-phone`} className="inp" type="tel" inputMode="tel" autoComplete="tel" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="(555) 010-0142" data-testid="settings-phone" />
          </div>
          <label className="check">
            <input type="checkbox" checked={sms} onChange={(e) => setSms(e.target.checked)} data-testid="settings-sms" />
            <span>
              <b>Text me on payday</b> with the amount, the card and a link. Message and data rates may apply. Reply STOP at any time.{!smsLive && " Texts are not being sent in this test set-up; they appear in the demo inbox instead."}
            </span>
          </label>
          <label className="check">
            <input type="checkbox" checked={email} onChange={(e) => setEmail(e.target.checked)} data-testid="settings-email" />
            <span>
              <b>Email me</b> when a statement is due for a check and before every renewal.
            </span>
          </label>
          <p className="fine" style={{ margin: 0 }}>
            Time zone: {prefs.timezone}
          </p>
        </div>
        {msg && (
          <p className={msg.ok ? "err okline" : "err"} role={msg.ok ? "status" : "alert"}>
            {msg.text}
          </p>
        )}
        <div className="row-btns">
          <button className="btn" type="submit" disabled={busy} data-testid="settings-save">
            {busy ? "Saving" : "Save reminders"}
          </button>
        </div>
      </form>
      {prefs.calendar_url && (
        <div style={{ marginTop: 20, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
          <h3>Calendar</h3>
          <p>Paydays, statement closing dates and due dates as a calendar you can subscribe to.</p>
          <div className="row-btns">
            <a className="ghost sm" href={prefs.calendar_url} download="burn-rate.ics" data-testid="settings-calendar">
              Add to calendar
            </a>
            <button className="textlink quiet" type="button" onClick={copy} style={{ fontSize: 14 }}>
              Copy the link
            </button>
            <span role="status" style={{ fontSize: 14, color: "var(--text-2)" }}>
              {copied ? "Copied." : ""}
            </span>
          </div>
        </div>
      )}
    </section>
  );
}

export default function Settings() {
  const { me, setMe, refresh, bump } = useSession();
  const navigate = useNavigate();
  const prefs = useLoad(() => api.prefs(), []);
  const [meter, setMeter] = useState<Meter | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const entitled = !!me?.entitled;

  useEffect(() => {
    if (!entitled) return;
    let live = true;
    api
      .meter()
      .then((m) => live && setMeter(m))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [entitled, me?.today]);

  if (!me) return <Loading />;
  const sub = me.subscription;

  const signOut = async () => {
    setSigningOut(true);
    try {
      await api.logout();
      const next = await api.session();
      setMe(next);
      bump();
      navigate("/", { replace: true });
    } catch (e) {
      setError(errorMessage(e));
      setSigningOut(false);
    }
  };

  return (
    <div className="set">
      <div className="pd-h">
        <div>
          <h1>Settings</h1>
          <p className="lead">{me.demo ? "A demo account with sample numbers. Changes here stay in the demo." : (me.user.email ?? "Your account")}</p>
        </div>
      </div>

      {!entitled && sub && (
        <p className="note" style={{ marginTop: 18 }} role="status">
          <b>Your plan is not active right now.</b>{" "}
          {sub.status === "refunded" || sub.status === "canceled" ? (
            <>
              To open Today, Cards, Asks and Statements again,{" "}
              <Link className="textlink" to="/plans">
                choose a plan
              </Link>
              .
            </>
          ) : (
            "Resume it below to open Today, Cards, Asks and Statements again."
          )}
        </p>
      )}

      <div style={{ marginTop: 18 }}>
        {sub ? (
          <PlanSection sub={sub} today={me.today} onChange={refresh} />
        ) : (
          <section className="tile">
            <div className="lab12">Your plan</div>
            <p style={{ marginTop: 4 }}>This session has access without a paid plan.</p>
          </section>
        )}

        {meter && (
          <section aria-label="Paid versus found" style={{ margin: "12px 0" }}>
            <MeterBox meter={meter} />
          </section>
        )}

        {prefs.data ? (
          <Notifications prefs={prefs.data} smsLive={me.config.sms_live} onSaved={prefs.setData} />
        ) : prefs.loading ? (
          <Loading />
        ) : (
          <ErrorBox message={prefs.error ?? "We couldn't load your reminder settings."} onRetry={prefs.reload} />
        )}

        <section className="tile" aria-label="Session">
          <div className="lab12">Session</div>
          <p style={{ marginTop: 4 }}>{me.demo ? "Leaving the demo clears its sample data from this browser session." : "You can sign in again with a link sent to your email."}</p>
          <div className="row-btns">
            <button className="ghost plain" type="button" onClick={signOut} disabled={signingOut} data-testid="settings-signout">
              {me.demo ? "Leave the demo" : "Sign out"}
            </button>
          </div>
          {error && <p className="err">{error}</p>}
        </section>
      </div>
    </div>
  );
}
