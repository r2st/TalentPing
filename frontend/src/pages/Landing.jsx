import { useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";

/* ---------- robot SVGs from DoAide template ---------- */

function RobotFace({ size = 32, color = "#F0B429" }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width={size} height={size}>
      <line x1="16" y1="6" x2="16" y2="2" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="16" cy="1.5" r="1.5" fill={color} />
      <rect x="5" y="6" width="22" height="17" rx="5" fill={color} />
      <ellipse cx="11" cy="13" rx="2.5" ry="3" fill="#0A0A0B" />
      <ellipse cx="21" cy="13" rx="2.5" ry="3" fill="#0A0A0B" />
      <circle cx="11.5" cy="12.5" r="1" fill={color} opacity="0.6" />
      <circle cx="21.5" cy="12.5" r="1" fill={color} opacity="0.6" />
      <path d="M12 19Q16 22 20 19" stroke="#0A0A0B" strokeWidth="1.2" fill="none" strokeLinecap="round" />
      <rect x="1" y="10" width="4" height="5" rx="2" fill={color} opacity="0.8" />
      <rect x="27" y="10" width="4" height="5" rx="2" fill={color} opacity="0.8" />
    </svg>
  );
}

function HeroRobot({ color = "#F0B429" }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 100" width="120" height="100" className="landing-hero-robot">
      <line x1="60" y1="18" x2="60" y2="6" stroke={color} strokeWidth="2.5" strokeLinecap="round" />
      <circle cx="60" cy="4" r="3" fill={color} className="landing-antenna-glow" />
      <rect x="25" y="18" width="70" height="55" rx="16" fill={color} />
      <ellipse cx="42" cy="40" rx="8" ry="10" fill="#0A0A0B" />
      <ellipse cx="78" cy="40" rx="8" ry="10" fill="#0A0A0B" />
      <circle cx="44" cy="38" r="3" fill={color} opacity="0.5" />
      <circle cx="80" cy="38" r="3" fill={color} opacity="0.5" />
      <path d="M45 60 Q60 72 75 60" stroke="#0A0A0B" strokeWidth="2.5" fill="none" strokeLinecap="round" />
      <rect x="5" y="30" width="16" height="18" rx="6" fill={color} opacity="0.8" />
      <rect x="99" y="30" width="16" height="18" rx="6" fill={color} opacity="0.8" />
    </svg>
  );
}

/* ---------- config from product-configs.json ---------- */

const ACCENT = "#F0B429";
const ACCENT_DARK = "#D4A017";

const FEATURES = [
  { icon: "\u{1F3AF}", title: "Smart Matching" },
  { icon: "⚡",    title: "Auto Apply" },
  { icon: "\u{1F4E7}", title: "Email Outreach" },
  { icon: "\u{1F4CA}", title: "Pipeline" },
];

const DOAIDE_PRODUCTS = [
  { name: "Desk",   url: "https://desk.doaide.com" },
  { name: "Jobs",   url: "https://job.doaide.com" },
  { name: "409A",   url: "https://409a.doaide.com" },
  { name: "GST",    url: "https://gst.doaide.com" },
  { name: "Pulse",  url: "https://pulse.doaide.com" },
  { name: "Med",    url: "https://med.doaide.com" },
  { name: "Realty", url: "https://realty.doaide.com" },
  { name: "Reach",  url: "https://reach.doaide.com" },
  { name: "Trade",  url: "https://trade.doaide.com" },
];

/* ---------- auth form ---------- */

function AuthForm() {
  const { login, register } = useAuth();
  const [mode, setMode] = useState("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const isSignUp = mode === "signup";

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
    <div className="landing-auth">
      <div className="landing-auth-tabs">
        <button
          type="button"
          className={`landing-auth-tab ${!isSignUp ? "landing-auth-tab-active" : ""}`}
          onClick={() => { setMode("signin"); setError(null); }}
        >
          Sign In
        </button>
        <button
          type="button"
          className={`landing-auth-tab ${isSignUp ? "landing-auth-tab-active" : ""}`}
          onClick={() => { setMode("signup"); setError(null); }}
        >
          Sign Up
        </button>
      </div>

      <form onSubmit={onSubmit} className="landing-auth-form">
        {isSignUp && (
          <div className="landing-auth-field">
            <label className="landing-auth-label" htmlFor="landing-name">Name (optional)</label>
            <input
              id="landing-name"
              className="landing-auth-input"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Read from your resume if blank"
              autoComplete="name"
            />
          </div>
        )}

        <div className="landing-auth-field">
          <label className="landing-auth-label" htmlFor="landing-email">Email</label>
          <input
            id="landing-email"
            type="email"
            required
            className="landing-auth-input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
            autoComplete="email"
          />
        </div>

        <div className="landing-auth-field">
          <label className="landing-auth-label" htmlFor="landing-password">Password</label>
          <input
            id="landing-password"
            type="password"
            required
            minLength={8}
            className="landing-auth-input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={isSignUp ? "At least 8 characters" : "••••••••"}
            autoComplete={isSignUp ? "new-password" : "current-password"}
          />
        </div>

        {error && <p className="landing-auth-error">{error}</p>}

        <button type="submit" className="landing-btn-primary landing-btn-lg" style={{ width: "100%" }} disabled={busy}>
          {busy ? "One moment…" : isSignUp ? "Create account" : "Sign in"}
        </button>
      </form>

      <p className="landing-auth-hint">
        {isSignUp ? "Free to start. No credit card." : "Welcome back."}
      </p>
    </div>
  );
}

/* ---------- main landing page ---------- */

export default function Landing() {
  const { user, loading } = useAuth();
  const [visible, setVisible] = useState(false);
  const authRef = useRef(null);

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true));
  }, []);

  if (loading) {
    return (
      <div className="landing-root" style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ color: "#71717A", fontSize: 12, textTransform: "uppercase", letterSpacing: "0.18em" }}>
          Loading
        </span>
      </div>
    );
  }

  if (user) return <Navigate to="/" replace />;

  function scrollToAuth() {
    authRef.current?.scrollIntoView({ behavior: "smooth" });
  }

  return (
    <div className="landing-root" style={{ "--accent": ACCENT, "--accent-dark": ACCENT_DARK }}>
      {/* Animated background */}
      <div className="landing-bg">
        <div className="landing-orb landing-orb-1" />
        <div className="landing-orb landing-orb-2" />
        <div className="landing-orb landing-orb-3" />
      </div>

      {/* Header */}
      <header className={`landing-header ${visible ? "landing-visible" : ""}`}>
        <a href="https://doaide.com" className="landing-brand" target="_blank" rel="noopener noreferrer">
          <RobotFace size={28} color={ACCENT} />
          <span className="landing-brand-text">
            Do<em>Aide</em> AutoApply
          </span>
        </a>
        <div className="landing-header-actions">
          <button type="button" className="landing-btn-ghost" onClick={scrollToAuth}>Sign in</button>
          <button type="button" className="landing-btn-primary" onClick={scrollToAuth}>Get started</button>
        </div>
      </header>

      {/* Hero */}
      <main className={`landing-hero ${visible ? "landing-visible" : ""}`}>
        <div className="landing-hero-robot-wrap">
          <HeroRobot color={ACCENT} />
        </div>
        <h1 className="landing-title">Apply to jobs while you sleep.</h1>
        <div className="landing-cta-group">
          <button type="button" className="landing-btn-primary landing-btn-lg" onClick={scrollToAuth}>Get started free</button>
          <button type="button" className="landing-btn-ghost landing-btn-lg" onClick={scrollToAuth}>Sign in</button>
        </div>
      </main>

      {/* Features */}
      <section className={`landing-features ${visible ? "landing-visible" : ""}`}>
        {FEATURES.map((f, i) => (
          <div
            key={f.title}
            className="landing-feature-card"
            style={{ animationDelay: `${0.3 + i * 0.1}s` }}
          >
            <span className="landing-feature-icon">{f.icon}</span>
            <span className="landing-feature-title">{f.title}</span>
          </div>
        ))}
      </section>

      {/* Auth section */}
      <section ref={authRef} className={`landing-auth-section ${visible ? "landing-visible" : ""}`}>
        <AuthForm />
      </section>

      {/* Footer */}
      <footer className="landing-footer">
        <div className="landing-footer-products">
          {DOAIDE_PRODUCTS.map((p) => (
            <a key={p.name} href={p.url} className="landing-footer-link" target="_blank" rel="noopener noreferrer">
              {p.name}
            </a>
          ))}
        </div>
        <div className="landing-footer-bottom">
          <a href="https://doaide.com" className="landing-footer-home" target="_blank" rel="noopener noreferrer">
            <RobotFace size={16} color={ACCENT} />
            doaide.com
          </a>
          <span className="landing-footer-copy">&copy; {new Date().getFullYear()} DoAide</span>
        </div>
      </footer>
    </div>
  );
}
