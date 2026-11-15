import { useNavigate } from "react-router-dom";
import { Logo } from "./Bits";
import { BackIcon } from "./Icons";

/** The thin bar on quiz, result, paywall and checkout: back, logo, step counter and a hairline progress bar. */
export default function FunnelTop({ step, of, onBack, backTo }: { step?: number; of?: number; onBack?: () => void; backTo?: string }) {
  const navigate = useNavigate();
  const back = () => {
    if (onBack) onBack();
    else if (backTo) navigate(backTo);
    else navigate(-1);
  };
  const pct = step && of ? Math.min(100, Math.round((step / of) * 100)) : 0;
  return (
    <>
      <header className="top">
        <button className="back" type="button" aria-label="Back" onClick={back} data-testid="quiz-back">
          <BackIcon />
        </button>
        <Logo />
        <div className="step num" aria-hidden={!step}>
          {step && of ? (
            <>
              <b>{step}</b> / {of}
            </>
          ) : null}
        </div>
      </header>
      {step && of ? (
        <div className="progress" role="progressbar" aria-label="Quiz progress" aria-valuemin={0} aria-valuemax={of} aria-valuenow={step}>
          <i style={{ width: `${pct}%` }} />
        </div>
      ) : null}
    </>
  );
}
