import { useEffect } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Projection } from "../api/types";
import { BigBurn, ErrorBox, Loading } from "../components/Bits";
import FunnelTop from "../components/FunnelTop";
import { dateLong, monthsText, monthYear, pct, usd, usdPerDay, usdRound, usdShort, usdTick } from "../lib/format";
import { useLoad } from "../lib/useLoad";
import { quizStart, useTicker } from "../lib/useTicker";

function PayoffTile({ label, p }: { label: string; p: Projection }) {
  return (
    <div>
      <div className="k">{label}</div>
      {p.never ? (
        <>
          <div className="v">Not paid off</div>
          <div className="d">At this pace the balance does not reach zero.</div>
        </>
      ) : (
        <>
          <div className="v">{p.debt_free_month ? monthYear(p.debt_free_month) : monthsText(p.months)}</div>
          <div className="d num">
            {monthsText(p.months)}, about {usdRound(p.interest_cents)} in interest
          </div>
        </>
      )}
    </div>
  );
}

export default function ResultPage() {
  const navigate = useNavigate();
  const { data: r, error, errorStatus, loading, reload } = useLoad(() => api.result(), []);
  const since = quizStart(false);
  const ticked = useTicker(r ? r.burn_per_day : null, since);

  useEffect(() => {
    if (r) api.track("result_viewed");
  }, [r]);

  if (errorStatus === 404) return <Navigate to="/quiz" replace />;
  if (!r) {
    return (
      <div className="app funnel">
        <FunnelTop backTo="/quiz" />
        <main className="body">{loading ? <Loading label="Counting" /> : <ErrorBox message={error ?? "We couldn't load your result."} onRetry={reload} />}</main>
      </div>
    );
  }

  const max = Math.max(...r.cards.map((c) => c.burn_per_day), 1);
  const move = r.first_move;
  const others = r.cards.length - 1;

  return (
    <div className="app funnel">
      <FunnelTop backTo="/quiz/autopay" />
      <main className="body" style={{ paddingTop: 16 }}>
        <h1 className="eyebrow">
          Your {r.cards.length === 1 ? "card" : `${r.cards.length} cards`}, right now
        </h1>
        <BigBurn value={r.burn_per_day} testId="result-burn" />
        <p className="since">
          <i className="dot" />
          <span>
            Since you started this quiz: <b className="num">{usdTick(ticked)}</b>
          </span>
        </p>

        <div className="rows num">
          <div>
            <span>Every paycheck ({r.paycheck_days} days)</span>
            <b>{usd(r.per_paycheck_cents)}</b>
          </div>
          <div>
            <span>Every year at this pace</span>
            <b>{usd(r.per_year_cents)}</b>
          </div>
        </div>

        <h2 className="h3">Where it burns</h2>
        <div className="split num">
          {r.cards.map((c) => (
            <div className="c" key={c.id}>
              <div className="nm">
                {c.name}
                <span>
                  {usdShort(c.balance_cents)} at {pct(c.apr_bps)} APR
                </span>
              </div>
              <div className="v">{usdPerDay(c.burn_per_day)} / day</div>
              <div className="bar" aria-hidden="true">
                <i style={{ width: `${Math.max(2, (c.burn_per_day / max) * 100)}%` }} />
              </div>
            </div>
          ))}
        </div>

        {move && (
          <div className="move" data-testid="result-first-move">
            <div className="when">Your first move · {dateLong(move.payday)}, payday</div>
            <p className="num">
              Send {usd(move.amount_cents)} to {move.card_name}.
            </p>
            <div className="after num">
              The burn drops to <b>{usdPerDay(move.burn_after)} a day</b>.
              {others > 0 && ` Autopay keeps your other ${others === 1 ? "card" : "cards"} on time.`}
              {move.short_by_cents > 0 && ` This paycheck is ${usd(move.short_by_cents)} short of the full plan; every minimum still comes first.`}
            </div>
          </div>
        )}

        {r.asks.length > 0 && (
          <>
            <h2 className="h3">Your first 14 days: {r.asks.length === 1 ? "one ask" : `${r.asks.length} asks`}</h2>
            <ul className="queue num">
              {r.asks.map((a, i) => (
                <li key={`${a.type}-${i}`}>
                  <i>{i + 1}</i>
                  <div>
                    {a.title}
                    <span>{a.detail}</span>
                  </div>
                  <b>{a.value_label}</b>
                </li>
              ))}
            </ul>
          </>
        )}

        <h2 className="h3">Debt-free date</h2>
        <div className="payoff">
          <PayoffTile label={`With the plan, ${usdShort(r.payoff.monthly_budget_cents)} a month`} p={r.payoff.plan} />
          <PayoffTile label="With minimum payments only" p={r.payoff.minimums} />
        </div>

        <div className="fine">
          Estimates from the numbers you entered. You make each call yourself; an issuer may say no.
          {r.assumptions.length > 0 && (
            <ul>
              {r.assumptions.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
          )}
        </div>
        <p className="fine">
          <Link className="textlink quiet" to="/quiz/count">
            Change my numbers
          </Link>
        </p>
      </main>
      <div className="cta-wrap">
        <button className="cta" type="button" onClick={() => navigate("/plans")} data-testid="result-cta">
          Get my payday plan
        </button>
      </div>
    </div>
  );
}
