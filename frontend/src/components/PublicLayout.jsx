import { useEffect } from "react";
import { Link } from "react-router-dom";

const ACCENT = "#F0B429";

function RobotFace({ size = 28 }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width={size} height={size}>
      <line x1="16" y1="6" x2="16" y2="2" stroke={ACCENT} strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="16" cy="1.5" r="1.5" fill={ACCENT} />
      <rect x="5" y="6" width="22" height="17" rx="5" fill={ACCENT} />
      <ellipse cx="11" cy="13" rx="2.5" ry="3" fill="#0A0A0B" />
      <ellipse cx="21" cy="13" rx="2.5" ry="3" fill="#0A0A0B" />
      <circle cx="11.5" cy="12.5" r="1" fill={ACCENT} opacity="0.6" />
      <circle cx="21.5" cy="12.5" r="1" fill={ACCENT} opacity="0.6" />
      <path d="M12 19Q16 22 20 19" stroke="#0A0A0B" strokeWidth="1.2" fill="none" strokeLinecap="round" />
    </svg>
  );
}

export default function PublicLayout({ title, description, jsonLd, children }) {
  useEffect(() => {
    if (title) document.title = `${title} | DoAide Jobs`;
    return () => { document.title = "DoAide Jobs"; };
  }, [title]);

  useEffect(() => {
    if (!jsonLd) return;
    const script = document.createElement("script");
    script.type = "application/ld+json";
    script.textContent = JSON.stringify(jsonLd);
    document.head.appendChild(script);
    return () => { document.head.removeChild(script); };
  }, [jsonLd]);

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "#0A0A0B", color: "#E4E4E7" }}>
      <header className="border-b" style={{ borderColor: "rgba(240,180,41,0.1)" }}>
        <div className="mx-auto max-w-5xl px-4 py-3 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2 no-underline">
            <RobotFace />
            <span className="text-lg font-bold text-white">
              DoAide <em style={{ color: ACCENT, fontStyle: "normal" }}>Jobs</em>
            </span>
          </Link>
          <nav className="flex items-center gap-4 text-sm">
            <Link to="/tools" className="text-zinc-400 hover:text-white transition-colors no-underline">Tools</Link>
            <Link to="/blog" className="text-zinc-400 hover:text-white transition-colors no-underline">Blog</Link>
            <Link to="/" className="rounded-lg px-4 py-1.5 text-sm font-medium no-underline" style={{ background: ACCENT, color: "#0A0A0B" }}>
              Get Started
            </Link>
          </nav>
        </div>
      </header>

      <main className="flex-1">
        <div className="mx-auto max-w-5xl px-4 py-8">
          {children}
        </div>
      </main>

      <div className="border-t" style={{ borderColor: "rgba(240,180,41,0.15)", background: "rgba(240,180,41,0.03)" }}>
        <div className="mx-auto max-w-5xl px-4 py-8 text-center">
          <h3 className="text-xl font-bold text-white mb-2">Ready to automate your job search?</h3>
          <p className="text-zinc-400 mb-4 text-sm">AI finds, matches, and applies to jobs for you.</p>
          <Link to="/" className="inline-block rounded-lg px-6 py-2.5 font-medium no-underline" style={{ background: ACCENT, color: "#0A0A0B" }}>
            Start Free
          </Link>
        </div>
      </div>

      <footer className="border-t py-6" style={{ borderColor: "rgba(255,255,255,0.06)" }}>
        <div className="mx-auto max-w-5xl px-4 flex items-center justify-between text-xs text-zinc-500">
          <div className="flex items-center gap-2">
            <RobotFace size={16} />
            <a href="https://doaide.com" className="hover:text-white transition-colors no-underline text-zinc-500" target="_blank" rel="noopener noreferrer">doaide.com</a>
          </div>
          <span>&copy; {new Date().getFullYear()} DoAide</span>
        </div>
      </footer>
    </div>
  );
}
