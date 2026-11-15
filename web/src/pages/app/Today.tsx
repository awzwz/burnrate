import { useState } from "react";
import { Link } from "react-router-dom";
import { api, errorMessage } from "../../api/client";
import type { Today } from "../../api/types";
import { ErrorBox, Loading, Plastic } from "../../components/Bits";
import InstructionCard from "../../components/InstructionCard";
import MeterBox from "../../components/MeterBox";
import { useShell } from "../../components/ProductShell";
import StepChart from "../../components/StepChart";
import { dateShort, usd, usdDelta, usdPerDay } from "../../lib/format";
import { useWide } from "../../lib/useWide";

export default function TodayPage() {
  const { today, loading, error, setToday, refresh } = useShell();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const wide = useWide();

  if (!today) return loading ? <Loading /> : <ErrorBox message={error ?? "We couldn't load today's plan."} onRetry={refresh} />;

  const run = async (call: () => Promise<Today>, event: string) => {
    setBusy(true);
    setActionError(null);
    try {
      setToday(await call());
      api.track(event);
    } catch (e) {
      setActionError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const ins = today.instruction;
  const burn = today.burn;
  const topRate = Math.max(0, ...today.cards.filter((c) => !c.paid_off).map((c) => c.apr_bps));
  const highest = today.cards.filter((c) => c.apr_bps === topRate).map((c) => c.id);
  const events = burn.history.filter((p) => p.delta !== 0).slice(-2);
  const ask = today.next_ask;

  return (
    <>
      <div className="pd-h">
        <div>
          <h1 data-testid="today-headline">{today.headline}</h1>
          <p className="lead">{today.lead}</p>
        </div>
        <span className="chip num" data-testid="today-burn" aria-label={`${usdPerDay(burn.now)} in interest a day`}>
          <i />
          {usdPerDay(burn.now)} / day
        </span>
      </div>

      <div className="pd-grid">
        <div className="col">
          {ins ? (
            <InstructionCard
              instruction={ins}
              today={today.date}
              highestRateCardIds={highest}
              busy={busy}
              error={actionError}
              onPaid={(amount) => run(() => api.markPaid(ins.id, amount), "instruction_paid")}
              onUndo={() => run(() => api.undoPaid(ins.id), "instruction_undone")}
              onShort={(available) => run(() => api.short(ins.id, available), "instruction_short")}
            />
          ) : (
            <section className="send blk-send">
              <div className="lab">
                <span>Nothing to send</span>
              </div>
              <p className="why" style={{ borderTop: 0, paddingTop: 0 }}>
                There is no payment to make right now. Add a card with a balance to get a payday plan.
              </p>
              <div className="btns">
                <Link className="ghost" to="/app/cards">
                  Go to cards
                </Link>
              </div>
            </section>
          )}

          {ask && (
            <div className="blk-ask">
              <div className="sec">
                <h2>Next ask</h2>
                {today.sprint && (
                  <span className="num">
                    {today.sprint.done} of {today.sprint.total} done this sprint
                  </span>
                )}
              </div>
              <div className="ask">
                <div className="k">{ask.title}</div>
                <div className="m num">{ask.value_label}</div>
                <div className="t">{ask.subtitle}</div>
                <div className="act">
                  <Link className="ghost" to={`/app/asks/${ask.id}?tab=rehearse`} data-testid="ask-rehearse">
                    Rehearse
                  </Link>
                  <Link className="ghost" to={`/app/asks/${ask.id}?tab=call`} data-testid="ask-callcard">
                    Open call card
                  </Link>
                </div>
              </div>
            </div>
          )}
        </div>

        <div className="col">
          <div className="blk-trend">
            <div className="sec">
              <h2>Burn per day</h2>
              <span>since {dateShort(burn.started_on)}</span>
            </div>
            <div className="trend">
              <div className="hd">
                <div className="now num">
                  {usdPerDay(burn.now)} <span>a day</span>
                </div>
                {Math.round(burn.down_by) > 0 && <div className="down num">↓ {usdPerDay(burn.down_by)} since you started</div>}
                {Math.round(burn.down_by) < 0 && <div className="down up num">↑ {usdPerDay(-burn.down_by)} since you started</div>}
              </div>
              <StepChart history={burn.history} today={today.date} now={burn.now} good={ins?.status === "paid"} />
              {events.length > 0 && (
                <ul className="ev num">
                  {events.map((p, i) => (
                    <li key={`${p.date}-${i}`}>
                      <b>
                        {dateShort(p.date)} · {p.label}
                      </b>
                      <span>{usdDelta(p.delta)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          {today.autopay.length > 0 && (
            <div className="blk-auto">
              <div className="sec">
                <h2>On autopay</h2>
                <span>minimums only</span>
              </div>
              <div className="auto num">
                {today.autopay.map((a) => (
                  <div key={a.card_id}>
                    <Plastic issuer={a.issuer} />
                    <div className="nm">
                      {a.card_name}
                      <span>{a.note}</span>
                    </div>
                    <div className="v">
                      {usd(a.min_cents)}
                      <span>{dateShort(a.due)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {!wide && (
            <div className="blk-meter">
              <div className="sec">
                <h2>Paid vs found</h2>
                <Link to="/app/settings">Details</Link>
              </div>
              <MeterBox meter={today.meter} compact />
            </div>
          )}
        </div>
      </div>
    </>
  );
}
