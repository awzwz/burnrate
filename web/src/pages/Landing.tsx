import { useEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { PlansResponse } from "../api/types";
import { BigBurn, Logo } from "../components/Bits";
import { Mark, Shield } from "../components/Icons";
import { usd, usdPerDay, usdShort, usdTick } from "../lib/format";
import { useTicker } from "../lib/useTicker";
import { useSession } from "../state/session";

// Sample inputs for the hero card. They are sent to POST /api/quiz/card-burn; the per-day figures come back
// from the server, and the card is labelled "sample data". The last row is the first card after the sample payment.
const SAMPLE_CARDS = [
  { name: "Capital One Quicksilver", balance_cents: 520000, apr_bps: 2799 },
  { name: "Chase Freedom Unlimited", balance_cents: 340000, apr_bps: 2249 },
  { name: "Discover it", balance_cents: 190000, apr_bps: 2499 },
];
const SAMPLE_TOTAL_CENTS = 1050000;
const SAMPLE_SEND_CENTS = 24000;
const SAMPLE_AFTER = { balance_cents: 496000, apr_bps: 2799 };

const STEPS = [
  { id: "step-enter", title: "Enter four numbers per card", text: "Balance, rate, minimum and due date from your latest statement. No bank login, no card number." },
  { id: "step-payday", title: "Get one instruction every payday", text: "One amount, one card, one day. Autopay keeps every minimum on time." },
  { id: "step-check", title: "Check it against your statement", text: "Each month we put our forecast next to the bank's own number and explain the difference." },
];

interface SampleBurn {
  now: number;
  after: number;
}

function useSampleBurn(ready: boolean): SampleBurn | null {
  const [burn, setBurn] = useState<SampleBurn | null>(null);
  useEffect(() => {
    if (!ready) return;
    let live = true;
    Promise.all([...SAMPLE_CARDS, SAMPLE_AFTER].map((c) => api.cardBurn(c.balance_cents, c.apr_bps)))
      .then((r) => {
        if (!live) return;
        const [a, b, c, afterFirst] = r.map((x) => x.burn_per_day);
        setBurn({ now: a + b + c, after: afterFirst + b + c });
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [ready]);
  return burn;
}

function SamplePanel({ burn }: { burn: SampleBurn }) {
  const opened = useRef(Date.now());
  const ticked = useTicker(burn.now, opened.current);
  return (
    <div className="panel">
      <div className="panel-h">
        <span>Three cards, {usdShort(SAMPLE_TOTAL_CENTS)}</span>
        <span className="tagline">sample data</span>
      </div>
      <BigBurn value={burn.now} />
      <p className="since">
        <i className="dot" />
        <span>
          Since you opened this page: <b className="num">{usdTick(ticked)}</b>
        </span>
      </p>
      <div className="mini">
        <div className="when">On payday</div>
        <p className="num">
          Send {usdShort(SAMPLE_SEND_CENTS)} to {SAMPLE_CARDS[0].name}.
        </p>
        <div className="after num">
          Then the burn is <b>{usdPerDay(burn.after)} a day</b>.
        </div>
      </div>
    </div>
  );
}

export default function Landing() {
  const { me, status, setMe, bump } = useSession();
  const navigate = useNavigate();
  const ready = status === "ready";
  const burn = useSampleBurn(ready);
  const [plans, setPlans] = useState<PlansResponse | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);
  const [demoError, setDemoError] = useState<string | null>(null);

  useEffect(() => {
    if (!ready) return;
    let live = true;
    api
      .plans()
      .then((p) => live && setPlans(p))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [ready]);

  const startDemo = async () => {
    setDemoBusy(true);
    setDemoError(null);
    try {
      const next = await api.demoStart();
      setMe(next);
      bump();
      api.track("demo_started");
      navigate("/app");
    } catch (e) {
      setDemoError(errorMessage(e));
      setDemoBusy(false);
    }
  };

  const jump = (id: string) => (e: MouseEvent) => {
    const target = document.getElementById(id);
    if (!target) return;
    e.preventDefault();
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
  };

  const signedIn = !!me?.entitled;

  return (
    <div className="app land">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <div className="a-gnav">
        <nav className="in" aria-label="Site">
          <Link to="/" className="keep mark" aria-label="Burn Rate">
            <Mark size={14} />
          </Link>
          <a href="#step-payday" onClick={jump("step-payday")}>
            Payday plan
          </a>
          <a href="#step-check" onClick={jump("step-check")}>
            Statement check
          </a>
          <a href="#pricing" onClick={jump("pricing")}>
            Pricing
          </a>
          <button type="button" onClick={startDemo} disabled={demoBusy}>
            Demo
          </button>
          {signedIn ? (
            <Link className="keep" to="/app">
              Open the app
            </Link>
          ) : (
            <Link className="keep" to="/login">
              Sign in
            </Link>
          )}
        </nav>
      </div>
      <div className="a-lnav">
        <div className="in">
          <b>Burn Rate</b>
          <nav aria-label="On this page">
            <a href="#main" aria-current="true" onClick={jump("main")}>
              Overview
            </a>
            <a href="#how" onClick={jump("how")}>
              How it works
            </a>
            <a href="#pricing" onClick={jump("pricing")}>
              Pricing
            </a>
          </nav>
          <Link className="a-pill" to="/quiz">
            See my number
          </Link>
        </div>
      </div>

      <main id="main">
        <section className="a-hero">
          <p className="a-eyebrow">
            <i className="mark">
              <Mark size={18} />
            </i>
            Burn Rate
          </p>
          <h1>
            See what your cards cost you. <br />
            Every single day.
          </h1>
          <p className="a-sub">One payment instruction per paycheck. Checked against your statement every month.</p>
          <div className="a-acts">
            <Link className="btn" to="/quiz" data-testid="start-quiz" onClick={() => api.track("quiz_cta")}>
              See my number
            </Link>
            <button className="ghost" type="button" onClick={startDemo} disabled={demoBusy} data-testid="try-demo">
              {demoBusy ? "Opening the demo" : "Try the demo"}
            </button>
          </div>
          <p className="a-under">
            The demo runs on sample numbers. No sign-up, no payment.{" "}
            <a href="#how" onClick={jump("how")}>
              How it works
            </a>
          </p>
          {demoError && (
            <p className="err" role="alert">
              {demoError}
            </p>
          )}
          <div className="a-card" aria-busy={!burn}>
            {burn ? (
              <SamplePanel burn={burn} />
            ) : (
              <p className="loading" role="status" style={{ padding: "24px 0" }}>
                {status === "error" ? "The live sample is not available right now." : "Loading the sample"}
              </p>
            )}
          </div>
        </section>

        <section className="a-lead" id="how">
          <p>
            Burn Rate reads the numbers on your statement, tells you which card to pay on payday, and shows its forecast next to your bank's
            figure. You make every payment and every call yourself.
          </p>
        </section>

        <section className="a-bento" aria-label="How it works">
          {STEPS.map((s, i) => (
            <div className="a-tile" key={s.id} id={s.id}>
              <span>Step {i + 1}</span>
              <b>{s.title}.</b>
              <p>{s.text}</p>
            </div>
          ))}
        </section>

        <section className="a-pricing" id="pricing">
          <h2>Choose your plan.</h2>
          {plans ? (
            <>
              <div className="tiers num">
                {plans.plans.map((p) => (
                  <div className={`tier ${p.tag ? "hl" : ""}`} key={p.id}>
                    {p.tag && <span className="tag">{p.tag}</span>}
                    <div className="nm">{p.name}</div>
                    <div className="pr">{usd(p.price_cents)}</div>
                    <div className="pd2">{usdPerDay(p.per_day_cents)} a day</div>
                    <div className="then">{p.then}</div>
                    <Link className={p.tag ? "btn" : "ghost"} to="/quiz" aria-label={`Start with the quiz, ${p.name}`}>
                      Start with the quiz
                    </Link>
                  </div>
                ))}
              </div>
              <p className="guar">
                <Shield /> {plans.guarantee_days}-day money-back on every plan. No conditions. Cancel online in two steps.
              </p>
            </>
          ) : (
            <p className="loading" role="status">
              {status === "error" ? "Prices are not available right now." : "Loading prices"}
            </p>
          )}
        </section>
      </main>

      <footer className="foot">
        <Logo size={18} />
        <p>
          Estimates are based on the numbers you enter; your statement has the final figure. You make each call yourself; an issuer may say no.
          Burn Rate is planning software. It is not a lender, it does not move money, and it does not contact your bank.
        </p>
        <nav aria-label="Footer">
          <Link to="/terms">Subscription terms</Link>
          <Link to="/login">Sign in</Link>
        </nav>
      </footer>
    </div>
  );
}
