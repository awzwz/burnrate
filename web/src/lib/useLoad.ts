import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage } from "../api/client";
import { useSession } from "../state/session";

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  /** The HTTP status of the failure, when there was one (404 lets a page say "not found"). */
  errorStatus: number | null;
  loading: boolean;
  reload: () => void;
  setData: (data: T) => void;
}

/** Loads one resource. Reloads when `deps` change and whenever the session's data version moves (demo clock). */
export function useLoad<T>(fetcher: () => Promise<T>, deps: unknown[] = []): Loaded<T> {
  const { version } = useSession();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const fn = useRef(fetcher);
  fn.current = fetcher;

  useEffect(() => {
    let live = true;
    setLoading(true);
    fn.current()
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
        setErrorStatus(null);
      })
      .catch((e) => {
        if (!live) return;
        setError(errorMessage(e));
        setErrorStatus(e instanceof ApiError ? e.status : null);
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, tick, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, errorStatus, loading, reload, setData };
}
