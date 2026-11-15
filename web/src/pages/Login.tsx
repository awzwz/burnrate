import { useId, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { InboxMessage } from "../api/types";
import FunnelTop from "../components/FunnelTop";
import { useSession } from "../state/session";

export default function Login() {
  const { me } = useSession();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [devLink, setDevLink] = useState<InboxMessage | null>(null);
  const id = useId();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const mail = email.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(mail)) return setError("Enter the email address you used when you started your plan.");
    setBusy(true);
    setError(null);
    try {
      await api.authLink(mail);
      setSent(true);
      // In test and demo set-ups nothing is emailed: the message lands in the inbox endpoint instead.
      if (me?.config.test_mode) {
        api
          .inbox()
          .then((r) => setDevLink(r.messages.find((m) => m.link && m.link.includes("/auth/verify") && m.to.toLowerCase() === mail.toLowerCase()) ?? null))
          .catch(() => undefined);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app funnel">
      <FunnelTop backTo="/" />
      {sent ? (
        <main className="body">
          <h1 className="title">Check your email</h1>
          <p className="sub">
            We sent a sign-in link to {email.trim()}. It works once.
          </p>
          {devLink?.link && (
            <div className="testnote" style={{ marginTop: 24 }}>
              <b>Test mode.</b> No email is sent here. The message is in the test inbox:{" "}
              <a className="textlink" href={devLink.link} data-testid="login-dev-link">
                open the sign-in link
              </a>
              .
            </div>
          )}
          <p className="fine center" style={{ marginTop: 24 }}>
            <button className="textlink quiet" type="button" onClick={() => setSent(false)}>
              Use a different email
            </button>
          </p>
        </main>
      ) : (
        <form onSubmit={submit} noValidate style={{ display: "contents" }}>
          <main className="body">
            <h1 className="title">Sign in</h1>
            <p className="sub">We email you a link. No password.</p>
            <div className="form">
              <div className="fld">
                <label htmlFor={`${id}-email`}>Email</label>
                <input id={`${id}-email`} className="inp" type="email" inputMode="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" aria-invalid={!!error} data-testid="login-email" />
              </div>
            </div>
            {error && (
              <p className="err" role="alert">
                {error}
              </p>
            )}
            <p className="fine center" style={{ marginTop: 24 }}>
              New here? <Link className="textlink" to="/quiz">See what your cards cost you per day</Link>
            </p>
          </main>
          <div className="cta-wrap">
            <button className="cta" type="submit" disabled={busy} data-testid="login-send">
              {busy ? "Sending" : "Email me a link"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
