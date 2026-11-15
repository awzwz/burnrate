import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { Ask } from "../../api/types";
import { ErrorBox, Loading } from "../../components/Bits";
import { dateShort } from "../../lib/format";
import { useLoad } from "../../lib/useLoad";

export const RESULT_TEXT: Record<string, string> = {
  granted: "Granted",
  partial: "Partly granted",
  temporary: "Temporary offer",
  refused: "Refused",
  no_answer: "No answer yet",
};

export function AskStatus({ ask }: { ask: Ask }) {
  if (ask.status === "queued") return null;
  if (ask.status === "skipped") return <span className="pill">Skipped</span>;
  const good = ask.result === "granted" || ask.result === "partial" || ask.result === "temporary";
  return <span className={`pill ${good ? "ok" : ""}`}>{ask.result ? (RESULT_TEXT[ask.result] ?? ask.result) : "Done"}</span>;
}

export default function Asks() {
  const { data, error, loading, reload } = useLoad(() => api.asks(), []);
  if (!data) return loading ? <Loading /> : <ErrorBox message={error ?? "We couldn't load your asks."} onRetry={reload} />;
  const { asks, sprint } = data;
  const next = asks.find((a) => a.status === "queued");

  return (
    <div className="narrow">
      <div className="pd-h">
        <div>
          <h1>Asks</h1>
          <p className="lead">A short list of things to ask your card issuers. You make each call yourself; an issuer may say no.</p>
        </div>
      </div>

      {sprint && (
        <div className="tile num" style={{ marginTop: 18 }} data-testid="sprint">
          <div className="lab12">Two-week sprint</div>
          <h3 style={{ marginTop: 4 }}>
            {sprint.done} of {sprint.total} done
          </h3>
          <div className="bar ok" style={{ marginTop: 10, background: "#dcdce1" }} role="img" aria-label={`${sprint.done} of ${sprint.total} asks done`}>
            <i style={{ width: `${sprint.total ? Math.round((sprint.done / sprint.total) * 100) : 0}%` }} />
          </div>
          <p style={{ marginTop: 8 }}>The sprint runs until {dateShort(sprint.ends_on)}. A no still counts as done: it is logged and your plan carries on.</p>
        </div>
      )}

      <div className="sec">
        <h2>Your queue</h2>
        <span>most likely first</span>
      </div>
      {asks.length === 0 ? (
        <p className="note">Nothing to ask right now. New asks appear when a statement shows a fee or a rate change.</p>
      ) : (
        <ol className="list num">
          {asks.map((a) => (
            <li key={a.id} className="link">
              <Link className="rowlink" to={`/app/asks/${a.id}`} data-testid="ask-item" aria-label={`${a.title}. ${a.subtitle}`}>
                <div className="nm">
                  {a.title} {a.id === next?.id && <span className="pill warn" style={{ marginLeft: 6 }}>Next</span>} <AskStatus ask={a} />
                  <small>{a.subtitle}</small>
                </div>
                <div className="v">{a.value_label}</div>
              </Link>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
