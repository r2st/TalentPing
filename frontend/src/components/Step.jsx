/**
 * One row of the setup wizard.
 *
 * The three steps are always visible so the user can see the whole path, but
 * only the active one is expanded — completed steps collapse to a single line
 * with a check and a summary, and future steps sit dimmed and inert. The active
 * marker carries a slow ping ring; it is the only moving thing on the page.
 */
export default function Step({ index, title, state, summary, children }) {
  const isActive = state === "active";
  const isDone = state === "done";

  return (
    <section
      className={[
        "panel relative overflow-hidden transition-all duration-300",
        isActive ? "shadow-lift" : "",
        // Upcoming steps recede via a flatter surface rather than opacity —
        // on a near-black canvas, dimming the whole element erases it.
        state === "todo" ? "bg-ink-800/40" : "",
      ].join(" ")}
      aria-current={isActive ? "step" : undefined}
    >
      {isActive && (
        <span className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-signal/60 to-transparent" />
      )}

      <div className="flex gap-4 p-5">
        <Marker index={index} isActive={isActive} isDone={isDone} />

        <div className="min-w-0 flex-1 pt-0.5">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h2
              className={[
                "text-[15px] font-semibold tracking-tight",
                isActive || isDone ? "text-white" : "text-white/45",
              ].join(" ")}
            >
              {title}
            </h2>
            {summary && (
              <span className="font-mono text-xs text-white/40">{summary}</span>
            )}
          </div>

          {children && <div className={isActive ? "mt-5" : "mt-3"}>{children}</div>}
        </div>
      </div>
    </section>
  );
}

function Marker({ index, isActive, isDone }) {
  if (isDone) {
    return (
      <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-good/15 text-good">
        <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" aria-hidden="true">
          <path
            d="M3.5 8.5l3 3 6-7"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="sr-only">Done</span>
      </span>
    );
  }

  return (
    <span className="relative mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center">
      {isActive && (
        <span className="absolute inset-0 rounded-full bg-signal/30 animate-ping-ring" />
      )}
      <span
        className={[
          "relative flex h-7 w-7 items-center justify-center rounded-full border font-mono text-xs",
          isActive
            ? "border-signal/50 bg-signal/15 text-signal"
            : "border-white/10 text-white/35",
        ].join(" ")}
      >
        {index}
      </span>
    </span>
  );
}
