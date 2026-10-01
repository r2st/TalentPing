/**
 * The expandable panel under a job card: what Scout made of the role, what the
 * market pays for it, and who the employer actually is.
 *
 * Loaded on expand rather than with the feed. All three halves are cached
 * server-side per role/market/employer, so the cost is real but paid once and
 * shared — and a hundred-row feed nobody expanded costs nothing at all.
 *
 * The design rule throughout: **say how confident we are, in the same breath as
 * the number.** A modelled salary band and an observed one look identical if
 * you only print the figures, and a candidate who negotiates against a number
 * we made up is worse off than one who never saw it. So every estimate is
 * labelled an estimate, and anything unknown is simply absent rather than
 * rendered as "unknown".
 */
import { useEffect, useState } from "react";

import { api } from "../lib/api";

/* -------------------------------------------------------------------------- */
/* Scout's take                                                               */
/* -------------------------------------------------------------------------- */

/**
 * The LLM re-rank, shown beside the deterministic score rather than instead of
 * it. Two numbers that disagree is the useful case — it means the posting reads
 * better (or worse) than its keyword overlap — so the panel never hides one.
 */
export function ScoutTake({ score, reasoning }) {
  if (score == null && !reasoning) return null;

  return (
    <section className="rounded-lg border border-signal/25 bg-signal/[0.06] p-3">
      <header className="flex items-center gap-2">
        <span className="badge bg-signal/20 text-signal">Scout's take</span>
        {score != null && (
          <span className="font-mono text-[13px] font-medium text-white">
            {Math.round(score)}
            <span className="text-white/35">/100</span>
          </span>
        )}
      </header>
      {reasoning && (
        <p className="mt-2 text-[12px] leading-relaxed text-white/70">{reasoning}</p>
      )}
      <p className="mt-2 text-[10px] text-white/30">
        A second opinion on career fit and growth. The score above it is the
        deterministic one and never changes.
      </p>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Salary                                                                     */
/* -------------------------------------------------------------------------- */

const VERDICT_STYLE = {
  above: ["above market", "text-good"],
  at: ["at market", "text-white/70"],
  below: ["below market", "text-warn"],
  unknown: ["not published", "text-white/35"],
};

function money(value, currency = "USD") {
  if (value == null) return null;
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      maximumFractionDigits: 0,
    }).format(value);
  } catch {
    // An unrecognised currency code shouldn't blank the card.
    return `${Math.round(value).toLocaleString()} ${currency}`;
  }
}

/**
 * The market band, with the posting's own number positioned against it.
 *
 * The bar is the whole point: a range in three numbers is hard to read, and
 * where *this* posting sits inside it is the only question the candidate has.
 */
export function SalaryCard({ salary }) {
  if (!salary?.band) return null;

  const { band, comparison, is_estimate: isEstimate } = salary;
  const [verdictLabel, verdictTone] =
    VERDICT_STYLE[comparison?.verdict] ?? VERDICT_STYLE.unknown;

  // Where the posting's midpoint falls across the band, clamped so an outlier
  // offer still renders on the bar instead of overflowing it.
  const span = band.max - band.min;
  const offered = comparison?.offered_mid;
  const markerPct =
    offered != null && span > 0
      ? Math.max(0, Math.min(100, ((offered - band.min) / span) * 100))
      : null;

  return (
    <section className="rounded-lg border border-white/[0.08] bg-white/[0.02] p-3">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-white/45">
          Market rate
        </h4>
        <span className={`text-[11px] ${verdictTone}`}>{verdictLabel}</span>
      </header>

      <div className="mt-2 flex items-baseline gap-2 font-mono">
        <span className="text-[13px] text-white/45">{money(band.min, band.currency)}</span>
        <span className="text-base font-medium text-white">
          {money(band.median, band.currency)}
        </span>
        <span className="text-[13px] text-white/45">{money(band.max, band.currency)}</span>
      </div>

      <div className="relative mt-2 h-1.5 rounded-full bg-white/[0.08]">
        <div className="absolute inset-y-0 left-1/4 right-1/4 rounded-full bg-white/20" />
        {markerPct != null && (
          <div
            className="absolute -top-1 h-3.5 w-0.5 rounded bg-signal"
            style={{ left: `${markerPct}%` }}
            aria-hidden="true"
          />
        )}
      </div>

      <p className="mt-2 text-[11px] leading-relaxed text-white/55">
        {comparison?.label}
        {offered != null && (
          <>
            {" "}
            This posting advertises{" "}
            <span className="font-mono text-white/75">
              {money(comparison.offered_min, band.currency)}
              {comparison.offered_max && comparison.offered_max !== comparison.offered_min
                ? `–${money(comparison.offered_max, band.currency)}`
                : ""}
            </span>
            .
          </>
        )}
      </p>

      <p className="mt-1.5 text-[10px] text-white/30">
        {band.role_label} · {band.seniority} · {band.location_label}
        {isEstimate && " · modelled estimate, not observed pay data"}
      </p>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Company                                                                    */
/* -------------------------------------------------------------------------- */

const FUNDING_LABEL = {
  bootstrapped: "Bootstrapped",
  seed: "Seed",
  series_a: "Series A",
  series_b: "Series B",
  series_c: "Series C",
  series_d: "Series D",
  series_e: "Series E",
  public: "Public",
  acquired: "Acquired",
};

function Fact({ label, value }) {
  // An unknown is omitted, never printed as "unknown" — a card of empty labels
  // reads as a broken lookup rather than an employer we know little about.
  if (!value) return null;
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wide text-white/35">{label}</dt>
      <dd className="mt-0.5 text-[12px] text-white/80">{value}</dd>
    </div>
  );
}

export function CompanyCard({ company, onRefresh, refreshing }) {
  if (!company) return null;

  const facts = [
    ["Industry", company.industry],
    ["Size", company.size ? `${company.size} people` : null],
    ["Founded", company.founded_year],
    ["HQ", company.headquarters],
    [
      "Funding",
      company.funding_stage && company.funding_stage !== "unknown"
        ? [FUNDING_LABEL[company.funding_stage] ?? company.funding_stage, company.funding_total]
            .filter(Boolean)
            .join(" · ")
        : null,
    ],
    ["Glassdoor", company.glassdoor_rating ? `${company.glassdoor_rating} / 5` : null],
  ];
  const known = facts.filter(([, value]) => value);

  return (
    <section className="rounded-lg border border-white/[0.08] bg-white/[0.02] p-3">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-white/45">
          {company.name}
        </h4>
        {onRefresh && (
          <button
            className="btn-quiet text-[11px]"
            onClick={onRefresh}
            disabled={refreshing}
          >
            {refreshing ? "Researching…" : "Refresh"}
          </button>
        )}
      </header>

      {company.summary && (
        <p className="mt-2 text-[12px] leading-relaxed text-white/70">{company.summary}</p>
      )}

      {known.length > 0 && (
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
          {known.map(([label, value]) => (
            <Fact key={label} label={label} value={value} />
          ))}
        </dl>
      )}

      {company.tech_stack?.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1">
          {company.tech_stack.slice(0, 10).map((tech) => (
            <span key={tech} className="badge bg-white/[0.06] text-white/55">
              {tech}
            </span>
          ))}
        </div>
      )}

      {company.news?.length > 0 && (
        <ul className="mt-3 space-y-1.5 border-t border-white/[0.06] pt-2.5">
          {company.news.map((item, i) => (
            <li key={`${item.title}-${i}`} className="text-[11px] leading-relaxed text-white/60">
              {item.title}
              {(item.published || item.source) && (
                <span className="text-white/30">
                  {" · "}
                  {[item.source, item.published].filter(Boolean).join(", ")}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}

      {known.length === 0 && !company.summary && (
        <p className="mt-2 text-[11px] text-white/35">
          Nothing reliable found about this employer yet.
        </p>
      )}

      {/* An `llm` card is recollection, not a lookup, and says so — the
          candidate is about to repeat this in an interview. */}
      {company.source === "llm" && (
        <p className="mt-2.5 text-[10px] text-white/30">
          Recalled by Scout rather than looked up. Worth confirming before you
          quote it back to them.
        </p>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Also on                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * The other boards carrying this exact role, collected by the dedup pass.
 *
 * Shown because the merge is otherwise invisible: a candidate who saw this job
 * on RemoteOK yesterday should be able to tell it's the same posting rather
 * than wondering where it went.
 */
export function AlsoOn({ links }) {
  if (!links?.length) return null;

  return (
    <p className="text-[11px] text-white/40">
      Also listed on{" "}
      {links.map((link, i) => (
        <span key={link.url}>
          {i > 0 && ", "}
          <a
            className="text-white/60 underline decoration-white/20 underline-offset-2 hover:text-white"
            href={link.url}
            target="_blank"
            rel="noreferrer noopener"
          >
            {link.source}
          </a>
        </span>
      ))}
      .
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* The panel                                                                  */
/* -------------------------------------------------------------------------- */

export default function JobIntel({ jobId }) {
  const [intel, setIntel] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");

    api
      .jobIntel(jobId)
      .then((data) => {
        if (!cancelled) setIntel(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    // The panel can be collapsed mid-flight; without this the response lands on
    // an unmounted component and React warns.
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  async function refresh() {
    setRefreshing(true);
    try {
      setIntel(await api.jobIntel(jobId, { refresh: true }));
    } catch (err) {
      setError(err.message);
    } finally {
      setRefreshing(false);
    }
  }

  if (loading) {
    return (
      <p className="px-4 py-3 text-[11px] text-white/35" role="status">
        Loading market and company context…
      </p>
    );
  }

  if (error) {
    return (
      <p className="px-4 py-3 text-[11px] text-bad/80" role="alert">
        {error}
      </p>
    );
  }

  if (!intel) return null;

  return (
    <div className="space-y-2.5 px-4 pb-4">
      <ScoutTake score={intel.llm_fit_score} reasoning={intel.llm_reasoning} />
      <SalaryCard salary={intel.salary} />
      <CompanyCard company={intel.company} onRefresh={refresh} refreshing={refreshing} />
      <AlsoOn links={intel.also_on} />
    </div>
  );
}
