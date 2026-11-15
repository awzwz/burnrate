import { api } from "../api/client";
import { ErrorBox, Loading } from "../components/Bits";
import FunnelTop from "../components/FunnelTop";
import { periodText, usd } from "../lib/format";
import { useLoad } from "../lib/useLoad";

/** Plain-language subscription terms. Prices and the refund window come from GET /api/billing/plans. */
export default function Terms() {
  const { data, error, loading, reload } = useLoad(() => api.plans(), []);
  return (
    <div className="app funnel">
      <FunnelTop backTo="/" />
      <main className="body prose">
        <h1 className="title" style={{ textAlign: "left" }}>
          Subscription terms
        </h1>
        <h2>What you pay</h2>
        {data ? (
          <ul className="num">
            {data.plans.map((p) => (
              <li key={p.id}>
                <b>{p.name}:</b> {usd(p.price_cents)} for the first {periodText(p.days)}, then {usd(p.renews_price_cents)} every {periodText(p.renews_days)} until you cancel.
              </li>
            ))}
          </ul>
        ) : loading ? (
          <Loading />
        ) : (
          <ErrorBox message={error ?? "Prices are not available right now."} onRetry={reload} />
        )}
        <p>The renewal price and period are shown next to the button before you pay, and you confirm them with a separate tick box. We email a reminder with the date and the amount before every renewal.</p>
        <h2>How to cancel</h2>
        <p>
          In the app, open Settings and choose Cancel plan, then confirm. Two steps. You keep access until the end of the period you paid for and nothing more is charged. You can also pause instead of cancelling.
        </p>
        <h2>Money back</h2>
        <p>{data ? `For ${data.guarantee_days} days after your first payment you can get a full refund in Settings, with no conditions and no questions.` : "You can get a full refund in Settings during the money-back window shown on the plans page."}</p>
        <h2>What Burn Rate does and doesn't do</h2>
        <ul>
          <li>It is planning software. Estimates come from the numbers you enter; your statement has the final figure.</li>
          <li>It never calls or messages your bank. You make each call yourself; an issuer may say no.</li>
          <li>It never moves money. You pay in your bank's own app or site.</li>
          <li>It is not a lender and offers no loans or cards.</li>
        </ul>
      </main>
    </div>
  );
}
