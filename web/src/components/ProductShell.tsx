import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { Link, Navigate, NavLink, Outlet, useLocation } from "react-router-dom";
import { api, ApiError, errorMessage } from "../api/client";
import type { Today } from "../api/types";
import { usdPerDay } from "../lib/format";
import { planLine } from "../lib/plan";
import { useWide } from "../lib/useWide";
import { useSession } from "../state/session";
import { Loading, Logo } from "./Bits";
import DemoBar from "./DemoBar";
import { AsksIcon, CardsIcon, SettingsIcon, StatementsIcon, TodayIcon } from "./Icons";
import MeterBox from "./MeterBox";

interface ShellValue {
  today: Today | null;
  loading: boolean;
  error: string | null;
  /** Replace the Today snapshot with one a mutation just returned. */
  setToday: (t: Today) => void;
  refresh: () => void;
}
const Ctx = createContext<ShellValue | null>(null);
export function useShell(): ShellValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useShell must be used inside ProductShell");
  return v;
}

const NAV = [
  { to: "/app", end: true, label: "Today", icon: TodayIcon, id: "nav-today" },
  { to: "/app/cards", end: false, label: "Cards", icon: CardsIcon, id: "nav-cards" },
  { to: "/app/asks", end: false, label: "Asks", icon: AsksIcon, id: "nav-asks" },
  { to: "/app/statements", end: false, label: "Statements", icon: StatementsIcon, id: "nav-statements" },
  { to: "/app/settings", end: false, label: "Settings", icon: SettingsIcon, id: "nav-settings" },
];

/** Where a visitor without access belongs: the paywall if they have a result, otherwise the start. */
function Gate() {
  const [to, setTo] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api
      .quiz()
      .then((q) => live && setTo(q.has_result ? "/plans" : "/"))
      .catch(() => live && setTo("/"));
    return () => {
      live = false;
    };
  }, []);
  return to ? <Navigate to={to} replace /> : <Loading />;
}

export default function ProductShell() {
  const { me, version, refresh: refreshMe } = useSession();
  const { pathname } = useLocation();
  const [today, setTodayState] = useState<Today | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const entitled = !!me?.entitled;
  const wide = useWide();

  useEffect(() => {
    if (!entitled) return;
    let live = true;
    setLoading(true);
    api
      .today()
      .then((t) => {
        if (!live) return;
        setTodayState(t);
        setError(null);
      })
      .catch((e) => {
        if (!live) return;
        setError(errorMessage(e));
        // Access ended on the server (period over, refund): re-read the account so the gate can route.
        if (e instanceof ApiError && e.status === 402) void refreshMe();
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [entitled, version, tick, refreshMe]);

  const setToday = useCallback((t: Today) => setTodayState(t), []);
  const refresh = useCallback(() => setTick((n) => n + 1), []);
  const value = useMemo(() => ({ today, loading, error, setToday, refresh }), [today, loading, error, setToday, refresh]);

  if (!me) return <Loading />;
  const settingsOnly = !entitled && !!me.subscription;
  if (!entitled && !settingsOnly) return <Gate />;
  if (settingsOnly && pathname !== "/app/settings") return <Navigate to="/app/settings" replace />;

  const sub = me.subscription;
  const who = me.demo ? me.demo.persona.charAt(0).toUpperCase() + me.demo.persona.slice(1) : (me.user.email ?? "Your account");
  const planText = sub ? planLine(sub) : "";
  const burn = today ? usdPerDay(today.burn.now) : null;

  return (
    <Ctx.Provider value={value}>
      <div className="app product">
        <a className="skip" href="#main">
          Skip to content
        </a>
        {wide && (
          <aside className="side" aria-label="Sidebar">
            <Logo size={20} to="/app" />
            <nav aria-label="Product">
              {NAV.map(({ to, end, label, icon: Icon, id }) => (
                <NavLink key={to} to={to} end={end} className={({ isActive }) => (isActive ? "on" : "")} data-testid={id}>
                  <Icon />
                  {label}
                </NavLink>
              ))}
            </nav>
            <div className="spacer" />
            {today && <MeterBox meter={today.meter} compact />}
            <Link className="acct" to="/app/settings">
              <i aria-hidden="true">{who.charAt(0).toUpperCase()}</i>
              <span>
                {who}
                <small className="num">{planText}</small>
              </span>
            </Link>
          </aside>
        )}
        <div className="main">
          {me.demo && <DemoBar />}
          <div className="bar2">
            <Logo size={18} to="/app" />
            {burn && (
              <span className="chip num" aria-label={`${burn} in interest a day`}>
                <i />
                {burn} / day
              </span>
            )}
          </div>
          <main className="pd" id="main">
            <Outlet />
          </main>
          {!wide && (
            <nav className="tabs" aria-label="Product">
              {NAV.map(({ to, end, label, icon: Icon, id }) => (
                <NavLink key={to} to={to} end={end} className={({ isActive }) => (isActive ? "on" : "")} data-testid={id}>
                  <Icon />
                  {label}
                </NavLink>
              ))}
            </nav>
          )}
        </div>
      </div>
    </Ctx.Provider>
  );
}
