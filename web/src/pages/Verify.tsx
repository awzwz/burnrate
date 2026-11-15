import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import { Logo } from "../components/Bits";
import { useSession } from "../state/session";

export default function Verify() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const { setMe, bump } = useSession();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(token ? null : "This sign-in link is incomplete. Ask for a new one.");
  const started = useRef(false);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true; // a link works once: never send it twice
    api
      .authVerify(token)
      .then(async (me) => {
        setMe(me);
        bump();
        if (me.entitled || me.subscription) return navigate("/app", { replace: true });
        const quiz = await api.quiz().catch(() => null);
        navigate(quiz?.has_result ? "/plans" : "/", { replace: true });
      })
      .catch((e) => setError(errorMessage(e)));
  }, [token, setMe, bump, navigate]);

  return (
    <div className="splash">
      <Logo size={22} />
      {error ? (
        <>
          <p role="alert">{error}</p>
          <Link className="btn" to="/login">
            Get a new link
          </Link>
        </>
      ) : (
        <p role="status">Signing you in</p>
      )}
    </div>
  );
}
