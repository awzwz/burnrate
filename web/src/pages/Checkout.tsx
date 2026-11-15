import { useId, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { Checkout as CheckoutT } from "../api/types";
import FunnelTop from "../components/FunnelTop";
import { usd } from "../lib/format";
import { useSession } from "../state/session";

function recall(id: string): CheckoutT | null {
  try {
    const raw = sessionStorage.getItem(`br-checkout-${id}`);
    return raw ? (JSON.parse(raw) as CheckoutT) : null;
  } catch {
    return null;
  }
}

const groupDigits = (v: string) => v.replace(/\D/g, "").slice(0, 19).replace(/(\d{4})(?=\d)/g, "$1 ");
const formatExp = (v: string) => {
  const d = v.replace(/\D/g, "").slice(0, 4);
  return d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
};

export default function Checkout() {
  const { id = "" } = useParams();
  const { me, setMe, bump } = useSession();
  const navigate = useNavigate();
  const checkout = useMemo(() => recall(id), [id]);
  const [number, setNumber] = useState("");
  const [exp, setExp] = useState("");
  const [cvc, setCvc] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const uid = useId();

  const testMode = checkout ? checkout.test_mode : !!me?.config.test_mode;
  const testCard = checkout?.test_card || me?.config.test_card || "";

  const fillTest = () => {
    const now = new Date();
    setNumber(groupDigits(testCard));
    setExp(`12/${String((now.getFullYear() + 2) % 100).padStart(2, "0")}`);
    setCvc("123");
    setError(null);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (number.replace(/\D/g, "").length < 12) return setError("Enter the card number.");
    if (!/^\d{2}\/\d{2}$/.test(exp)) return setError("Enter the expiry date as MM/YY.");
    if (!/^\d{3,4}$/.test(cvc)) return setError("Enter the 3 or 4 digit security code.");
    setBusy(true);
    setError(null);
    try {
      const next = await api.pay(id, { card_number: number, exp, cvc });
      setMe(next);
      bump();
      api.track("paid");
      navigate("/app", { replace: true });
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <div className="app funnel">
      <FunnelTop backTo="/plans" />
      <form onSubmit={submit} noValidate style={{ display: "contents" }}>
        <main className="body" style={{ paddingTop: 16 }}>
          <h1 className="title">Payment</h1>
          {checkout ? (
            <div className="summary num">
              <div className="l">
                <span>{checkout.plan.name}</span>
                <span>{usd(checkout.amount_cents)}</span>
              </div>
              <p style={{ marginTop: 4 }}>
                Today {usd(checkout.amount_cents)}, {checkout.plan.then}, until you cancel in Settings.
              </p>
            </div>
          ) : (
            <p className="sub">
              Your plan and its renewal terms are the ones you confirmed on the <Link to="/plans">plans page</Link>.
            </p>
          )}

          {testMode && testCard && (
            <div className="testnote" data-testid="checkout-test-note">
              <b>Test mode.</b> Use card <b className="num">{testCard}</b>, any future date, any CVC. No real charge is made.{" "}
              <button className="textlink" type="button" onClick={fillTest} data-testid="checkout-fill-test">
                Fill it in
              </button>
            </div>
          )}

          <div className="form">
            <div className="fld">
              <label htmlFor={`${uid}-n`}>Card number</label>
              <input id={`${uid}-n`} className="inp num" inputMode="numeric" autoComplete="cc-number" placeholder="1234 5678 9012 3456" value={number} onChange={(e) => setNumber(groupDigits(e.target.value))} data-testid="checkout-number" />
            </div>
            <div className="two">
              <div className="fld">
                <label htmlFor={`${uid}-e`}>Expiry (MM/YY)</label>
                <input id={`${uid}-e`} className="inp num" inputMode="numeric" autoComplete="cc-exp" placeholder="MM/YY" value={exp} onChange={(e) => setExp(formatExp(e.target.value))} data-testid="checkout-exp" />
              </div>
              <div className="fld">
                <label htmlFor={`${uid}-c`}>CVC</label>
                <input id={`${uid}-c`} className="inp num" inputMode="numeric" autoComplete="cc-csc" placeholder="123" value={cvc} onChange={(e) => setCvc(e.target.value.replace(/\D/g, "").slice(0, 4))} data-testid="checkout-cvc" />
              </div>
            </div>
          </div>
          {error && (
            <p className="err" role="alert">
              {error}
            </p>
          )}
        </main>
        <div className="cta-wrap">
          <button className="cta" type="submit" disabled={busy} data-testid="checkout-pay">
            {busy ? "Paying" : checkout ? `Pay ${usd(checkout.amount_cents)}` : "Pay"}
          </button>
          <p className="pay-note">You can cancel online in Settings in two steps.</p>
        </div>
      </form>
    </div>
  );
}
