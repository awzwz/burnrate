import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, errorMessage } from "../api/client";
import type { Checkout, Result } from "../api/types";
import { ErrorBox, Loading } from "../components/Bits";
import FunnelTop from "../components/FunnelTop";
import PaywallForm from "../components/PaywallForm";
import { useLoad } from "../lib/useLoad";
import { useSession } from "../state/session";

export function rememberCheckout(c: Checkout): void {
  try {
    sessionStorage.setItem(`br-checkout-${c.id}`, JSON.stringify(c));
  } catch {
    /* private mode: the checkout page falls back to what it can load */
  }
}

export default function Plans() {
  const { me, setMe, bump } = useSession();
  const navigate = useNavigate();
  const plans = useLoad(() => api.plans(), []);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .result()
      .then((r) => live && setResult(r))
      .catch(() => undefined); // no result yet: the page still shows the plans
    api.track("paywall_viewed");
    return () => {
      live = false;
    };
  }, []);

  const start = async (planId: string, email: string) => {
    setError(null);
    try {
      const checkout = await api.checkout(planId, email);
      api.track("checkout_started", { plan: planId });
      if (checkout.url) {
        window.location.assign(checkout.url);
        return;
      }
      rememberCheckout(checkout);
      navigate(`/checkout/${checkout.id}`);
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  const demo = async () => {
    setDemoBusy(true);
    try {
      setMe(await api.demoStart());
      bump();
      navigate("/app");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : errorMessage(e));
      setDemoBusy(false);
    }
  };

  return (
    <div className="app funnel">
      <FunnelTop backTo={result ? "/result" : "/"} />
      {plans.data ? (
        <>
          <PaywallForm
            plans={plans.data.plans}
            guaranteeDays={plans.data.guarantee_days}
            burnPerDay={result ? result.burn_per_day : null}
            defaultEmail={me?.user.email ?? ""}
            testMode={plans.data.test_mode}
            onStart={start}
            error={error}
          />
          {!me?.entitled && (
            <p className="pay-note" style={{ margin: "-8px 16px 32px" }}>
              Just looking?{" "}
              <button className="textlink" type="button" onClick={demo} disabled={demoBusy}>
                Open the demo with sample numbers
              </button>
              . No payment.
            </p>
          )}
        </>
      ) : (
        <main className="body">{plans.loading ? <Loading /> : <ErrorBox message={plans.error ?? "We couldn't load the plans."} onRetry={plans.reload} />}</main>
      )}
    </div>
  );
}
