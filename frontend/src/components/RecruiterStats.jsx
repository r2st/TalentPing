import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";

/**
 * What the inbox watcher has actually been doing.
 *
 * The panel exists because the product could describe its *decisions* in detail
 * — every message carries its classification, its route and its reason — and
 * could say nothing at all about its *work*. "Is this thing running?" and "why
 * didn't it find that one?" were unanswerable without a log search.
 *
 * Three deliberate choices:
 *
 * **Scanned is the honest denominator.** It is how many messages Gmail returned
 * for the search, not how many were opened. It is much larger than the number
 * examined, and that gap is the whole point of the filters — showing the small
 * number would flatter the product and mislead the user.
 *
 * **Skips are named in English.** A list that says `skipped_own_thread: 402` is
 * a log line. "Conversations you started: 402" is an answer.
 *
 * **Auto-sent is called out separately from replied.** Those are the messages
 * that went out without the user reading them, and that number should never be
 * buried inside a larger one.
 *
 * The chart is inline SVG on purpose. The project has three runtime
 * dependencies, and this is a dozen rectangles.
 */

const RANGES = [
  [7, "7 days", "day"],
  [30, "30 days", "day"],
  [90, "90 days", "week"],
];

export default function RecruiterStats({ onError }) {
  const [range, setRange] = useState(30);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const [days, , bucket] = RANGES.find(([d]) => d === range) ?? RANGES[1];
    setLoading(true);
    try {
      setData(await api.recruiterStats({ days, bucket }));
    } catch (err) {
      // A stats panel that fails must not take the inbox down with it — the
      // messages are the page, this is the footnote.
      onError?.(err.message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [range, onError]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !data) {
    return (
      <section className="panel px-5 py-6" aria-busy="true">
        <p className="eyebrow">Activity</p>
        <p className="mt-3 text-sm text-white/30">Loading…</p>
      </section>
    );
  }

  if (!data) return null;

  const t = data.totals ?? {};
  const trend = data.trend ?? [];
  const skipped = data.skipped ?? [];

  return (
    <section className="panel space-y-6 px-5 py-6" aria-label="Activity">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="eyebrow">Activity</p>
          <p className="mt-1 text-xs text-white/35">
            {t.scans === 0
              ? "No checks recorded in this window yet."
              : `${t.scans.toLocaleString()} ${t.scans === 1 ? "check" : "checks"} of your mailbox.`}
          </p>
        </div>
        <div className="flex items-center gap-1" role="group" aria-label="Time range">
          {RANGES.map(([days, label]) => (
            <button
              key={days}
              type="button"
              aria-pressed={range === days}
              onClick={() => setRange(days)}
              className={[
                "chip transition-colors",
                range === days
                  ? "border-signal/40 bg-signal/10 text-white"
                  : "text-white/50 hover:text-white/80",
              ].join(" ")}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Tile label="Scanned" value={t.listed} hint="Messages checked in range" />
        <Tile label="Detected" value={t.detected} hint="New to us" />
        <Tile
          label="From recruiters"
          value={t.classified_recruiter}
          hint="A real person about a real role"
        />
        <Tile label="Replied" value={t.replied} />
        <Tile
          label="Sent for you"
          value={t.auto_sent}
          hint="Went out without your review"
          tone={t.auto_sent > 0 ? "text-signal" : undefined}
        />
        <Tile label="Awaiting you" value={(t.drafts_pending ?? 0) + (t.flagged ?? 0)} />
      </dl>

      {t.escalated > 0 && (
        <p className="text-xs text-white/45">
          {t.escalated} {t.escalated === 1 ? "recruiter has" : "recruiters have"}{" "}
          written back since we answered — those are waiting for you rather than
          for another automatic reply.
        </p>
      )}

      <TrendBars points={trend} bucket={data.bucket} />

      {skipped.length > 0 && (
        <div className="space-y-2 border-t pt-5 hairline">
          <p className="eyebrow">Why messages were passed over</p>
          <ul className="space-y-1">
            {skipped.map((row) => (
              <li
                key={row.reason}
                className="flex items-baseline justify-between gap-4 text-xs"
              >
                <span className="text-white/50">{row.label}</span>
                <span className="font-mono text-white/35">
                  {row.count.toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
          {t.deferred > 0 && (
            <p className="pt-1 text-[11px] text-white/30">
              {t.deferred.toLocaleString()} left for the next check — nothing is
              dropped, it just waits its turn.
            </p>
          )}
        </div>
      )}

      <PushState push={data.push} />
    </section>
  );
}

function Tile({ label, value, hint, tone }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-white/35">{label}</dt>
      <dd
        className={[
          "mt-0.5 font-display text-2xl leading-none tracking-tight",
          tone || "text-white",
        ].join(" ")}
      >
        {(value ?? 0).toLocaleString()}
      </dd>
      {hint && <p className="mt-1 text-[11px] leading-snug text-white/25">{hint}</p>}
    </div>
  );
}

/**
 * Detections per bucket, as bars.
 *
 * Deliberately plots *detections* rather than messages scanned. Scanned is
 * dominated by mail that was skipped for being already-seen, so its shape is a
 * picture of how often the scan ran — which is not a thing anybody wants to
 * look at. Detections are the thing that varies for an interesting reason.
 */
export function TrendBars({ points = [], bucket = "day" }) {
  if (points.length === 0) {
    return (
      <p className="text-xs text-white/30">
        Nothing detected in this window yet.
      </p>
    );
  }

  const peak = Math.max(1, ...points.map((p) => p.detected ?? 0));
  const width = Math.max(points.length * 14, 120);

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between">
        <p className="eyebrow">Detected per {bucket}</p>
        <span className="font-mono text-[10px] text-white/30">peak {peak}</span>
      </div>
      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${width} 48`}
          className="h-12 w-full min-w-[120px]"
          preserveAspectRatio="none"
          role="img"
          aria-label={`Detected per ${bucket}: ${points
            .map((p) => `${p.label} ${p.detected ?? 0}`)
            .join(", ")}`}
        >
          {points.map((point, index) => {
            const value = point.detected ?? 0;
            const height = Math.max(value > 0 ? 2 : 1, (value / peak) * 44);
            return (
              <rect
                key={point.period ?? index}
                data-testid="trend-bar"
                x={index * 14 + 3}
                y={48 - height}
                width={8}
                height={height}
                rx={1.5}
                fill={value > 0 ? "#f0b429" : "rgba(255,255,255,0.08)"}
              />
            );
          })}
        </svg>
      </div>
      <div className="flex justify-between font-mono text-[10px] text-white/25">
        <span>{points[0]?.label}</span>
        {points.length > 1 && <span>{points[points.length - 1]?.label}</span>}
      </div>
    </div>
  );
}

/**
 * Whether the mailbox is being pushed to, or swept on a timer.
 *
 * "Registered" and "delivering" are separate claims, and the UI says which one
 * is failing. A subscription can look perfectly healthy while Google has quietly
 * stopped publishing to it — that is exactly the case the five-minute fallback
 * sweep exists for, and the honest thing is to say so rather than to show a
 * green light.
 */
function PushState({ push }) {
  if (!push || !push.configured) return null;

  const message = push.covering
    ? "Gmail is notifying us as mail arrives, so replies are drafted within seconds."
    : push.healthy
      ? "Push is registered but has been quiet, so your mailbox is being checked every few minutes instead."
      : "Push isn't running, so your mailbox is being checked every few minutes instead.";

  return (
    <div className="border-t pt-5 text-xs hairline">
      <div className="flex items-center gap-2">
        <span
          className={[
            "h-1.5 w-1.5 shrink-0 rounded-full",
            push.covering ? "bg-good" : "bg-white/25",
          ].join(" ")}
          aria-hidden="true"
        />
        <span className="text-white/45">{message}</span>
      </div>
      {push.scans_from_push + push.scans_from_beat > 0 && (
        <p className="mt-1 pl-3.5 font-mono text-[10px] text-white/25">
          {push.scans_from_push} from push · {push.scans_from_beat} scheduled ·{" "}
          {push.scans_manual} on demand
        </p>
      )}
    </div>
  );
}
