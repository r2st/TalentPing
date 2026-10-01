import { useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import Logo from "../components/ui/Logo";

/* ---------- background visual elements ---------- */

function FloatingOrbs() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      <div className="orb orb-1" />
      <div className="orb orb-2" />
      <div className="orb orb-3" />
    </div>
  );
}

function GridPattern() {
  return (
    <svg
      className="pointer-events-none absolute inset-0 h-full w-full opacity-[0.025]"
      aria-hidden="true"
    >
      <defs>
        <pattern id="grid" width="48" height="48" patternUnits="userSpaceOnUse">
          <path d="M48 0H0v48" fill="none" stroke="white" strokeWidth="0.4" />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill="url(#grid)" />
    </svg>
  );
}

function ParticleField() {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    let frame;
    let particles = [];

    function resize() {
      canvas.width = canvas.offsetWidth * window.devicePixelRatio;
      canvas.height = canvas.offsetHeight * window.devicePixelRatio;
      ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    }

    function init() {
      resize();
      const w = canvas.offsetWidth;
      const h = canvas.offsetHeight;
      particles = Array.from({ length: 40 }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.3,
        vy: (Math.random() - 0.5) * 0.3,
        r: Math.random() * 1.5 + 0.5,
        alpha: Math.random() * 0.3 + 0.1,
      }));
    }

    function draw() {
      const w = canvas.offsetWidth;
      const h = canvas.offsetHeight;
      ctx.clearRect(0, 0, w, h);

      for (const p of particles) {
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < 0) p.x = w;
        if (p.x > w) p.x = 0;
        if (p.y < 0) p.y = h;
        if (p.y > h) p.y = 0;

        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(240, 180, 41, ${p.alpha})`;
        ctx.fill();
      }

      for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
          const dx = particles[i].x - particles[j].x;
          const dy = particles[i].y - particles[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < 120) {
            ctx.beginPath();
            ctx.moveTo(particles[i].x, particles[i].y);
            ctx.lineTo(particles[j].x, particles[j].y);
            ctx.strokeStyle = `rgba(240, 180, 41, ${0.06 * (1 - dist / 120)})`;
            ctx.lineWidth = 0.5;
            ctx.stroke();
          }
        }
      }

      frame = requestAnimationFrame(draw);
    }

    init();
    draw();
    window.addEventListener("resize", init);
    return () => {
      window.removeEventListener("resize", init);
      cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 h-full w-full"
      aria-hidden="true"
    />
  );
}

/* ---------- animated hero elements ---------- */

function PulsingRing({ delay = 0 }) {
  return (
    <div
      className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-signal/20"
      style={{
        width: "180px",
        height: "180px",
        animation: `landing-pulse 3.6s cubic-bezier(0, 0, 0.2, 1) infinite`,
        animationDelay: `${delay}s`,
      }}
    />
  );
}

function AnimatedLogo() {
  return (
    <div className="relative flex items-center justify-center">
      <PulsingRing delay={0} />
      <PulsingRing delay={1.2} />
      <PulsingRing delay={2.4} />
      <div className="landing-logo-float relative z-10">
        <Logo className="h-20 w-20 text-signal drop-shadow-[0_0_32px_rgba(240,180,41,0.5)] md:h-24 md:w-24" />
      </div>
    </div>
  );
}

function TypewriterText({ words, className = "" }) {
  const [index, setIndex] = useState(0);
  const [displayed, setDisplayed] = useState("");
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    const word = words[index];
    const speed = deleting ? 35 : 70;

    if (!deleting && displayed === word) {
      const pause = setTimeout(() => setDeleting(true), 2200);
      return () => clearTimeout(pause);
    }
    if (deleting && displayed === "") {
      setDeleting(false);
      setIndex((i) => (i + 1) % words.length);
      return;
    }

    const timer = setTimeout(() => {
      setDisplayed(
        deleting ? word.slice(0, displayed.length - 1) : word.slice(0, displayed.length + 1)
      );
    }, speed);
    return () => clearTimeout(timer);
  }, [displayed, deleting, index, words]);

  return (
    <span className={className}>
      {displayed}
      <span className="animate-pulse text-signal">|</span>
    </span>
  );
}

function StepIndicator({ number, text, delay }) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setVisible(true), delay);
    return () => clearTimeout(t);
  }, [delay]);

  return (
    <div
      className={`flex items-center gap-3 transition-all duration-700 ${
        visible ? "translate-y-0 opacity-100" : "translate-y-4 opacity-0"
      }`}
    >
      <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full border border-signal/30 bg-signal/10 font-mono text-xs font-medium text-signal">
        {number}
      </div>
      <span className="text-sm text-white/50">{text}</span>
    </div>
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
    <div className="w-full">
      <div className="mb-5 flex gap-1 rounded-lg border border-white/[0.06] bg-ink-900/60 p-1">
        <button
          type="button"
          className={`flex-1 rounded-md px-3 py-2 text-sm font-medium transition-all duration-200 ${
            !isSignUp
              ? "bg-signal text-ink-900 shadow-sm"
              : "text-white/50 hover:text-white/80"
          }`}
          onClick={() => { setMode("signin"); setError(null); }}
        >
          Sign In
        </button>
        <button
          type="button"
          className={`flex-1 rounded-md px-3 py-2 text-sm font-medium transition-all duration-200 ${
            isSignUp
              ? "bg-signal text-ink-900 shadow-sm"
              : "text-white/50 hover:text-white/80"
          }`}
          onClick={() => { setMode("signup"); setError(null); }}
        >
          Sign Up
        </button>
      </div>

      <form onSubmit={onSubmit} className="space-y-3">
        {isSignUp && (
          <div className="animate-fade-up">
            <label className="label" htmlFor="landing-name">
              Name <span className="normal-case tracking-normal">(optional)</span>
            </label>
            <input
              id="landing-name"
              className="input"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Read from your resume if blank"
              autoComplete="name"
            />
          </div>
        )}

        <div>
          <label className="label" htmlFor="landing-email">Email</label>
          <input
            id="landing-email"
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
          <label className="label" htmlFor="landing-password">Password</label>
          <input
            id="landing-password"
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

      <p className="mt-4 text-center text-xs text-white/25">
        {isSignUp
          ? "Free to start. No credit card required."
          : "Welcome back. Your pipeline awaits."}
      </p>
    </div>
  );
}

/* ---------- main landing page ---------- */

export default function Landing() {
  const { user, loading } = useAuth();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <span className="eyebrow animate-pulse">Loading</span>
      </div>
    );
  }

  if (user) return <Navigate to="/" replace />;

  return (
    <div className="relative flex min-h-screen flex-col overflow-hidden">
      <FloatingOrbs />
      <GridPattern />
      <ParticleField />

      {/* Top bar */}
      <header className="relative z-20 flex items-center justify-between px-6 py-5 md:px-12">
        <div className="flex items-center gap-3">
          <Logo className="h-8 w-8 text-signal" />
          <span className="font-display text-xl tracking-tightest text-white">
            DoAide<span className="italic text-signal"> AutoApply</span>
          </span>
        </div>
      </header>

      {/* Main content */}
      <main className="relative z-10 flex flex-1 flex-col items-center justify-center gap-14 px-6 pb-16 pt-4 lg:flex-row lg:gap-20 lg:px-20">
        {/* Left: Hero */}
        <div
          className={`flex max-w-lg flex-1 flex-col items-center text-center transition-all duration-700 lg:items-start lg:text-left ${
            mounted ? "translate-y-0 opacity-100" : "translate-y-8 opacity-0"
          }`}
        >
          <div className="mb-8">
            <AnimatedLogo />
          </div>

          <h1 className="font-display text-4xl leading-[1.05] tracking-tightest text-white md:text-5xl lg:text-[3.5rem]">
            Your AI{" "}
            <TypewriterText
              words={["recruiter finder", "email writer", "reply tracker", "job hunter"]}
              className="text-signal"
            />
          </h1>

          <p className="mt-4 max-w-sm text-sm leading-relaxed text-white/35">
            Upload a resume. We handle the rest.
          </p>

          <div className="mt-10 space-y-3">
            <StepIndicator number="1" text="Upload your resume" delay={800} />
            <StepIndicator number="2" text="We find recruiters and write emails" delay={1100} />
            <StepIndicator number="3" text="Track replies on autopilot" delay={1400} />
          </div>
        </div>

        {/* Right: Auth form */}
        <div
          className={`w-full max-w-[380px] flex-shrink-0 transition-all delay-200 duration-700 ${
            mounted ? "translate-y-0 opacity-100" : "translate-y-8 opacity-0"
          }`}
        >
          <div className="rounded-2xl border border-white/[0.08] bg-ink-800/70 p-6 shadow-lift backdrop-blur-md md:p-8">
            <h2 className="mb-1 font-display text-2xl tracking-tightest text-white">
              Get started
            </h2>
            <p className="mb-6 text-sm text-white/30">
              Your next opportunity is one upload away.
            </p>
            <AuthForm />
          </div>
        </div>
      </main>

      {/* Footer */}
      <footer className="relative z-10 border-t border-white/[0.06] px-6 pb-6 pt-5 text-center">
        <div className="mb-3 flex flex-wrap justify-center gap-x-4 gap-y-1 font-mono text-[10px] uppercase tracking-[0.14em]">
          <a href="https://desk.doaide.com" target="_blank" rel="noopener noreferrer" className="text-white/20 transition-colors hover:text-signal">Desk</a>
          <a href="https://herald.doaide.com" target="_blank" rel="noopener noreferrer" className="text-white/20 transition-colors hover:text-signal">Herald</a>
          <a href="https://409.doaide.com" target="_blank" rel="noopener noreferrer" className="text-white/20 transition-colors hover:text-signal">409A</a>
          <span className="text-signal">AutoApply</span>
          <a href="https://homenex.doaide.com" target="_blank" rel="noopener noreferrer" className="text-white/20 transition-colors hover:text-signal">Realty</a>
        </div>
        <p className="text-xs text-white/20">
          © {new Date().getFullYear()}{" "}
          <a href="https://doaide.com" target="_blank" rel="noopener noreferrer" className="transition-colors hover:text-white/40">DoAide</a>
          {" "}· AI tools for small businesses
        </p>
      </footer>
    </div>
  );
}
