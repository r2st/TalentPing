import { useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import Logo from "../components/ui/Logo";

/**
 * One page for both signing in and signing up — the distinction is a toggle,
 * not a separate route. Name is optional: we read it off the resume anyway.
 */
export default function Login() {
  const { user, login, register } = useAuth();
  const [mode, setMode] = useState("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const isSignUp = mode === "signup";

  if (user) return <Navigate to="/" replace />;

  async function onSubmit(event) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (isSignUp) await register(email, password, fullName || null);
      else await login(email, password);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-5 py-16">
      <div className="stagger w-full max-w-[380px]">
        <div className="mb-10 text-center">
          <Logo className="mx-auto mb-5 h-12 w-12 text-signal" />
          <h1 className="font-display text-[42px] leading-none tracking-tightest text-white">
            DoAide<span className="italic text-signal"> AutoApply</span>
          </h1>
          <p className="mt-4 text-sm leading-relaxed text-white/45">
            Upload a resume. We find the recruiters,
            <br />
            write the emails, and track the replies.
          </p>
        </div>

        <form onSubmit={onSubmit} className="panel space-y-4 p-6 shadow-lift">
          {isSignUp && (
            <div>
              <label className="label" htmlFor="name">
                Name <span className="normal-case tracking-normal">(optional)</span>
              </label>
              <input
                id="name"
                className="input"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                placeholder="Read from your resume if blank"
                autoComplete="name"
              />
            </div>
          )}

          <div>
            <label className="label" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              className="input font-mono"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
            />
          </div>

          <div>
            <label className="label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              minLength={8}
              className="input font-mono"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={isSignUp ? "At least 8 characters" : "••••••••"}
              autoComplete={isSignUp ? "new-password" : "current-password"}
            />
          </div>

          {error && (
            <p className="rounded-lg border border-bad/25 bg-bad/10 px-3 py-2 text-xs text-bad">
              {error}
            </p>
          )}

          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy ? "One moment…" : isSignUp ? "Create account" : "Sign in"}
          </button>
        </form>

        <p className="mt-6 text-center text-sm text-white/35">
          {isSignUp ? "Already have an account?" : "First time here?"}{" "}
          <button
            type="button"
            className="text-signal underline-offset-4 hover:underline"
            onClick={() => {
              setMode(isSignUp ? "signin" : "signup");
              setError(null);
            }}
          >
            {isSignUp ? "Sign in" : "Create one"}
          </button>
        </p>
      </div>
    </div>
  );
}
