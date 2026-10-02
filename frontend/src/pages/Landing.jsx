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
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 180" width="200" height="180" className="landing-hero-robot">
      <line x1="100" y1="30" x2="100" y2="10" stroke={color} strokeWidth="3" strokeLinecap="round" />
      <circle cx="100" cy="7" r="5" fill={color} className="landing-antenna-glow" />
      <rect x="40" y="30" width="120" height="90" rx="24" fill={color} />
      <ellipse cx="70" cy="68" rx="14" ry="17" fill="#0A0A0B" />
      <ellipse cx="130" cy="68" rx="14" ry="17" fill="#0A0A0B" />
      <circle cx="73" cy="64" r="5" fill={color} opacity="0.5" />
      <circle cx="133" cy="64" r="5" fill={color} opacity="0.5" />
      <path d="M75 100 Q100 118 125 100" stroke="#0A0A0B" strokeWidth="3.5" fill="none" strokeLinecap="round" />
      <rect x="8" y="50" width="26" height="30" rx="10" fill={color} opacity="0.8" />
      <rect x="166" y="50" width="26" height="30" rx="10" fill={color} opacity="0.8" />
      <rect x="55" y="125" width="90" height="30" rx="10" fill={color} opacity="0.9" />
      <rect x="65" y="158" width="12" height="18" rx="5" fill={color} opacity="0.7" />
      <rect x="123" y="158" width="12" height="18" rx="5" fill={color} opacity="0.7" />
    </svg>
  );
}

/* ---------- config ---------- */

const ACCENT = "#F0B429";
const ACCENT_DARK = "#D4A017";

const TYPEWRITER_PHRASES = [
  "Smart job matching",
  "Automated applications",
  "AI resume tailoring",
  "Interview-ready pipeline",
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

/* ---------- particles ---------- */

function Particles() {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    let animId;
    let particles = [];

    function resize() {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    }
    resize();
    window.addEventListener("resize", resize);

    for (let i = 0; i < 40; i++) {
      particles.push({
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        r: Math.random() * 2 + 0.5,
        dx: (Math.random() - 0.5) * 0.3,
        dy: (Math.random() - 0.5) * 0.3,
        opacity: Math.random() * 0.4 + 0.1,
      });
    }

    function draw() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (const p of particles) {
        p.x += p.dx;
        p.y += p.dy;
        if (p.x < 0) p.x = canvas.width;
        if (p.x > canvas.width) p.x = 0;
        if (p.y < 0) p.y = canvas.height;
        if (p.y > canvas.height) p.y = 0;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(240, 180, 41, ${p.opacity})`;
        ctx.fill();
      }
      animId = requestAnimationFrame(draw);
    }
    draw();

    return () => {
      cancelAnimationFrame(animId);
      window.removeEventListener("resize", resize);
    };
  }, []);

  return <canvas ref={canvasRef} className="landing-particles" />;
}

/* ---------- typewriter ---------- */

function Typewriter({ phrases }) {
  const [phraseIdx, setPhraseIdx] = useState(0);
  const [charIdx, setCharIdx] = useState(0);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    const phrase = phrases[phraseIdx];
    let timeout;

    if (!deleting && charIdx < phrase.length) {
      timeout = setTimeout(() => setCharIdx(charIdx + 1), 70);
    } else if (!deleting && charIdx === phrase.length) {
      timeout = setTimeout(() => setDeleting(true), 2000);
    } else if (deleting && charIdx > 0) {
      timeout = setTimeout(() => setCharIdx(charIdx - 1), 40);
    } else if (deleting && charIdx === 0) {
      setDeleting(false);
      setPhraseIdx((phraseIdx + 1) % phrases.length);
    }

    return () => clearTimeout(timeout);
  }, [charIdx, deleting, phraseIdx, phrases]);

  return (
    <span className="landing-typewriter">
      <span className="landing-typewriter-text">
        {phrases[phraseIdx].slice(0, charIdx)}
      </span>
      <span className="landing-typewriter-cursor" />
    </span>
  );
}

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
          Create Account
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

        <button type="submit" className="landing-btn-primary landing-btn-form" disabled={busy}>
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

  return (
    <div className="landing-root" style={{ "--accent": ACCENT, "--accent-dark": ACCENT_DARK }}>
      <Particles />

      {/* Header */}
      <header className={`landing-header ${visible ? "landing-visible" : ""}`}>
        <a href="https://doaide.com" className="landing-brand" target="_blank" rel="noopener noreferrer">
          <RobotFace size={28} color={ACCENT} />
          <span className="landing-brand-text">
            Do<em>Aide</em>
          </span>
        </a>
      </header>

      {/* Split layout */}
      <main className={`landing-split ${visible ? "landing-visible" : ""}`}>
        {/* Left panel — product info */}
        <div className="landing-left">
          <div className="landing-left-content">
            <div className="landing-hero-robot-wrap">
              <HeroRobot color={ACCENT} />
            </div>

            <h1 className="landing-headline">
              Job applications, <em>automated.</em>
            </h1>

            <p className="landing-subtitle">
              AI finds, matches, and applies to jobs for you — while you focus on interviews.
            </p>

            <div className="landing-typewriter-wrap">
              <Typewriter phrases={TYPEWRITER_PHRASES} />
            </div>
          </div>
        </div>

        {/* Right panel — auth */}
        <div className="landing-right">
          <AuthForm />
        </div>
      </main>

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
