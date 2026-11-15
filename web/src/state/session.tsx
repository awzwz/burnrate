import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api/client";
import type { Me } from "../api/types";

interface SessionValue {
  me: Me | null;
  /** "loading" until POST /api/session answers; "error" when it could not be reached. */
  status: "loading" | "ready" | "error";
  setMe: (me: Me) => void;
  refresh: () => Promise<Me | null>;
  retry: () => void;
  /** Bumped whenever server-side state jumped (demo clock, sign-in): pages reload their data. */
  version: number;
  bump: () => void;
}

const Ctx = createContext<SessionValue | null>(null);

export function SessionProvider({ children, initial }: { children: ReactNode; initial?: Me | null }) {
  const [me, setMeState] = useState<Me | null>(initial ?? null);
  const [status, setStatus] = useState<SessionValue["status"]>(initial !== undefined ? "ready" : "loading");
  const [version, setVersion] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const started = useRef(-1);

  useEffect(() => {
    if (initial !== undefined) return;
    if (started.current === attempt) return; // StrictMode runs effects twice in dev
    started.current = attempt;
    setStatus("loading");
    api
      .session()
      .then((m) => {
        setMeState(m);
        setStatus("ready");
      })
      .catch(() => setStatus("error"));
  }, [attempt, initial]);

  const setMe = useCallback((m: Me) => {
    setMeState(m);
    setStatus("ready");
  }, []);
  const bump = useCallback(() => setVersion((v) => v + 1), []);
  const refresh = useCallback(async () => {
    try {
      const m = await api.me();
      setMeState(m);
      setStatus("ready");
      return m;
    } catch {
      return null;
    }
  }, []);
  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  const value = useMemo(() => ({ me, status, setMe, refresh, retry, version, bump }), [me, status, setMe, refresh, retry, version, bump]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): SessionValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSession must be used inside SessionProvider");
  return v;
}
