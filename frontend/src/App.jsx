import { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Shell from "./components/Shell";
import { useAuth } from "./hooks/useAuth";
import { api } from "./lib/api";
import Inbox from "./pages/Inbox";
import Jobs from "./pages/Jobs";
import Login from "./pages/Login";
import Pipeline from "./pages/Pipeline";
import Setup from "./pages/Setup";
import Tailor from "./pages/Tailor";

function Loading() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <span className="eyebrow animate-pulse">Loading</span>
    </div>
  );
}

function Protected({ children }) {
  const { user, loading } = useAuth();
  if (loading) return <Loading />;
  if (!user) return <Navigate to="/login" replace />;
  return <Shell>{children}</Shell>;
}

/**
 * Where "/" goes depends on who you are. A new user has nothing to look at on
 * the pipeline — an empty funnel is not a first impression — so they land on
 * setup. Once setup is complete the pipeline is the page worth opening, and
 * being sent back to a wizard you already finished reads as broken.
 *
 * The onboarding read is fast, but this still renders a spinner rather than
 * guessing: flashing the wrong page and then redirecting is worse than a beat
 * of nothing.
 */
function Home() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api
      .onboarding()
      // A failed read shouldn't strand the user on a spinner. Setup is the
      // safe destination: it is readable whatever state the account is in.
      .catch(() => ({ complete: false }))
      .then((data) => !cancelled && setStatus(data));
    return () => {
      cancelled = true;
    };
  }, []);

  if (!status) return <Loading />;
  return <Navigate to={status.complete ? "/pipeline" : "/setup"} replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* The four destinations in the nav. */}
      <Route
        path="/pipeline"
        element={
          <Protected>
            <Pipeline />
          </Protected>
        }
      />
      <Route
        path="/jobs"
        element={
          <Protected>
            <Jobs />
          </Protected>
        }
      />
      <Route
        path="/inbox"
        element={
          <Protected>
            <Inbox />
          </Protected>
        }
      />
      <Route
        path="/setup"
        element={
          <Protected>
            <Setup />
          </Protected>
        }
      />

      {/* Reachable, but not a top-level destination: tailoring is something you
          do *to a job*, so it is entered from the feed rather than the nav. */}
      <Route
        path="/tailor"
        element={
          <Protected>
            <Tailor />
          </Protected>
        }
      />

      <Route
        path="/"
        element={
          <Protected>
            <Home />
          </Protected>
        }
      />

      {/* Old destinations, folded into the four above. */}
      <Route path="/dashboard" element={<Navigate to="/pipeline" replace />} />
      <Route path="/tracker" element={<Navigate to="/pipeline" replace />} />
      {/* Autopilot was a preferences form; its switch lives on the pipeline
          header and its fields in setup. */}
      <Route path="/autopilot" element={<Navigate to="/pipeline" replace />} />
      {/* Review was a second list of the same drafts the inbox already held. */}
      <Route path="/review" element={<Navigate to="/inbox?tab=drafts" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
