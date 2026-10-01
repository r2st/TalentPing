/**
 * A single inline notice. Replaces the half-dozen hand-rolled banners that were
 * copy-pasted across pages. `tone` picks the palette; `bad` is the default
 * because errors were the common case.
 */
const TONES = {
  bad: "border-bad/25 bg-bad/10 text-bad",
  signal: "border-signal/25 bg-signal/[0.08] text-signal",
  warn: "border-warn/25 bg-warn/10 text-warn",
};

export default function ErrorBanner({ children, tone = "bad", onDismiss, className = "" }) {
  if (!children) return null;
  return (
    <div
      role={tone === "bad" ? "alert" : "status"}
      className={[
        "flex items-start justify-between gap-3 rounded-lg border px-4 py-3 text-sm",
        TONES[tone] ?? TONES.bad,
        className,
      ].join(" ")}
    >
      <span className="min-w-0">{children}</span>
      {onDismiss && (
        <button
          className="shrink-0 opacity-60 transition-opacity hover:opacity-100"
          onClick={onDismiss}
          aria-label="Dismiss"
        >
          ×
        </button>
      )}
    </div>
  );
}
