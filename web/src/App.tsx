import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Logo } from "./components/Bits";
import ProductShell from "./components/ProductShell";
import Checkout from "./pages/Checkout";
import Landing from "./pages/Landing";
import Login from "./pages/Login";
import NotFound from "./pages/NotFound";
import Plans from "./pages/Plans";
import ResultPage from "./pages/Result";
import Terms from "./pages/Terms";
import Verify from "./pages/Verify";
import AskDetailPage from "./pages/app/AskDetail";
import Asks from "./pages/app/Asks";
import CardDetail from "./pages/app/CardDetail";
import Cards from "./pages/app/Cards";
import Settings from "./pages/app/Settings";
import StatementDetail from "./pages/app/StatementDetail";
import Statements from "./pages/app/Statements";
import TodayPage from "./pages/app/Today";
import Quiz from "./pages/quiz/Quiz";
import { useSession } from "./state/session";

function ScrollTop() {
  const { pathname } = useLocation();
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);
  return null;
}

export default function App() {
  const { status, retry } = useSession();
  const { pathname } = useLocation();

  // The landing renders even if the API is slow or down; everything else waits for the session cookie.
  if (status !== "ready" && pathname !== "/") {
    return (
      <div className="splash">
        <Logo size={22} />
        {status === "loading" ? (
          <p role="status">Loading</p>
        ) : (
          <>
            <p role="alert">We couldn't reach Burn Rate. Check your connection and try again.</p>
            <button className="btn" type="button" onClick={retry}>
              Try again
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    <>
      <ScrollTop />
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/quiz" element={<Quiz />} />
        <Route path="/quiz/:step" element={<Quiz />} />
        <Route path="/result" element={<ResultPage />} />
        <Route path="/plans" element={<Plans />} />
        <Route path="/checkout/:id" element={<Checkout />} />
        <Route path="/login" element={<Login />} />
        <Route path="/auth/verify" element={<Verify />} />
        <Route path="/terms" element={<Terms />} />
        <Route path="/app" element={<ProductShell />}>
          <Route index element={<TodayPage />} />
          <Route path="cards" element={<Cards />} />
          <Route path="cards/:id" element={<CardDetail />} />
          <Route path="asks" element={<Asks />} />
          <Route path="asks/:id" element={<AskDetailPage />} />
          <Route path="statements" element={<Statements />} />
          <Route path="statements/:id" element={<StatementDetail />} />
          <Route path="settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/app" replace />} />
        </Route>
        <Route path="*" element={<NotFound />} />
      </Routes>
    </>
  );
}
