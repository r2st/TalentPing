/**
 * Fit score display — a ring for the headline number, bars for the breakdown.
 *
 * The colour is the message: amber (the app's signal) means "act on this", and
 * everything weaker recedes to grey rather than competing for attention. Red is
 * reserved for genuinely poor matches, because a wall of red on a job feed just
 * teaches people to ignore the colour.
 */

const BANDS = [
  { min: 80, key: "strong", label: "strong match", tone: "text-good", ring: "#4ade80" },
  { min: 65, key: "good", label: "good match", tone: "text-signal", ring: "#f0b429" },
  { min: 45, key: "stretch", label: "a stretch", tone: "text-warn", ring: "#fb923c" },
  { min: 0, key: "poor", label: "weak match", tone: "text-bad", ring: "#f87171" },
];

export function bandFor(score) {
  return BANDS.find((b) => (score ?? 0) >= b.min) ?? BANDS[BANDS.length - 1];
}

/** The headline number as a progress ring. */
export function FitRing({ score, size = 92, label = true }) {
  const value = Math.max(0, Math.min(100, score ?? 0));
  const band = bandFor(value);
  const radius = (size - 10) / 2;
  const circumference = 2 * Math.PI * radius;

  return (
    <div className="flex items-center gap-4">
      <div className="relative shrink-0" style={{ width: size, height: size }}>
        <svg
          viewBox={`0 0 ${size} ${size}`}
          className="-rotate-90"
          width={size}
          height={size}
          aria-hidden="true"
        >
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="rgba(255,255,255,0.08)"
            strokeWidth="5"
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={band.ring}
            strokeWidth="5"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - value / 100)}
            style={{ transition: "stroke-dashoffset 700ms cubic-bezier(0.16,1,0.3,1)" }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span
            className="font-display leading-none tracking-tightest text-white"
            style={{ fontSize: size * 0.34 }}
          >
            {Math.round(value)}
          </span>
        </div>
      </div>
      {label && (
        <div className="min-w-0">
          <p className={`text-sm font-medium ${band.tone}`}>{band.label}</p>
          <p className="eyebrow mt-1">fit score</p>
        </div>
      )}
    </div>
  );
}

/** A compact score pill for dense lists (the job feed). */
export function FitPill({ score }) {
  if (score === null || score === undefined) {
    return <span className="badge bg-white/[0.06] text-white/30">unscored</span>;
  }
  const band = bandFor(score);
  return (
    <span className={`badge bg-white/[0.06] ${band.tone}`}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: band.ring }} />
      {Math.round(score)}
    </span>
  );
}

const DIMENSION_LABELS = {
  skills: "Skills",
  experience: "Experience",
  location: "Location",
  salary: "Salary",
  industry: "Industry",
};

/**
 * Per-dimension bars. Each row shows its weight, because "location scored 40"
 * matters much less than "skills scored 40" and the user deserves to see why.
 */
export function FitBreakdown({ breakdown }) {
  const rows = Object.entries(breakdown ?? {}).sort(
    ([, a], [, b]) => b.weight - a.weight,
  );
  if (!rows.length) return null;

  return (
    <ul className="space-y-3">
      {rows.map(([key, dim]) => (
        <li key={key}>
          <div className="flex items-baseline justify-between gap-3">
            <span className="text-sm text-white/75">
              {DIMENSION_LABELS[key] ?? key}
              <span className="ml-2 font-mono text-[10px] text-white/25">
                {Math.round(dim.weight * 100)}%
              </span>
            </span>
            <span className="font-mono text-xs text-white/50">
              {Math.round(dim.score)}
            </span>
          </div>
          <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-white/[0.06]">
            <div
              className="h-full rounded-full transition-all duration-700"
              style={{
                width: `${Math.max(0, Math.min(100, dim.score))}%`,
                background: bandFor(dim.score).ring,
              }}
            />
          </div>
          {dim.note && (
            <p className="mt-1.5 text-xs leading-relaxed text-white/35">{dim.note}</p>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * The skills split. The missing list is the honest half of the product — it is
 * shown as prominently as the matched one, never buried.
 */
export function KeywordSplit({ matched = [], missing = [] }) {
  if (!matched.length && !missing.length) return null;
  return (
    <div className="grid gap-5 sm:grid-cols-2">
      <div>
        <p className="eyebrow">You match ({matched.length})</p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {matched.length ? (
            matched.map((skill) => (
              <span key={skill} className="chip border-good/25 text-good/90">
                {skill}
              </span>
            ))
          ) : (
            <span className="text-xs text-white/30">Nothing overlapped.</span>
          )}
        </div>
      </div>
      <div>
        <p className="eyebrow">Not on your resume ({missing.length})</p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {missing.length ? (
            missing.map((skill) => (
              <span key={skill} className="chip border-white/10 text-white/40">
                {skill}
              </span>
            ))
          ) : (
            <span className="text-xs text-white/30">You cover everything listed.</span>
          )}
        </div>
        {missing.length > 0 && (
          <p className="mt-2 text-xs leading-relaxed text-white/30">
            We never write these into your resume. Address them yourself if you
            genuinely have the experience.
          </p>
        )}
      </div>
    </div>
  );
}
