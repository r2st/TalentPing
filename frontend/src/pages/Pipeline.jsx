import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ErrorBanner from "../components/ui/ErrorBanner";
import SkeletonLoader from "../components/ui/SkeletonLoader";
import Tooltip from "../components/ui/Tooltip";
import { useToast } from "../components/ui/Toast";
import { LIVE_CAMPAIGN_STATUSES } from "../lib/constants";
import { formatWhen } from "../lib/format";
import { api } from "../lib/api";

const STAGE_TONE = ["#f0b429", "#e5a623", "#4ade80", "#4ade80", "#4ade80"];

const EVENT_STYLE = {
  outreach_sent: ["sent", "text-sky-300"],
  follow_up_sent: ["follow-up", "text-signal"],
  reply_received: ["reply", "text-good"],
};

const EMPTY_FILTERS = { company: "", campaign_id: "", status: "", days: "" };

/**
 * Pipeline — the single "where every application stands" page, merged from the
 * old Dashboard, Tracker and Autopilot. One dashboard request drives the funnel,
 * table and feed so they can never disagree; the campaign strip (with
 * pause/resume and live polling) sits above them, filtering all three at once.
 *
 * Autopilot's master switch lives in the header here rather than on a page of
 * its own: it was a preferences form with one control anybody actually reached
 * for, and that control belongs next to the thing it produces. The rest of its
 * fields stay in setup, which doubles as the settings screen.
 */
export default function Pipeline() {
  const toast = useToast();
  const [data, setData] = useState(null);
  const [queue, setQueue] = useState(null);
  const [campaigns, setCampaigns] = useState([]);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    const [dashboard, upcoming, campaignList] = await Promise.all([
      api.dashboard(filters),
      api.dashboardQueue(),
      api.listCampaigns(),
    ]);
    setData(dashboard);
    setQueue(upcoming);
    setCampaigns(campaignList);
    return campaignList;
  }, [filters]);

  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, [load]);

  // While a campaign is mid-pipeline the counters change under us; poll until
  // everything settles, then stop.
  useEffect(() => {
    if (!campaigns.some((c) => LIVE_CAMPAIGN_STATUSES.has(c.status))) return undefined;
    const timer = setInterval(() => load().catch(() => {}), 5000);
    return () => clearInterval(timer);
  }, [campaigns, load]);

  if (!data) return <SkeletonLoader rows={[96, 224, 256]} />;

  const { stats, pipeline, applications, activity, filters: options } = data;
  const hasFilter = Object.values(filters).some(Boolean);

  return (
    <div className="space-y-10">
      <header className="animate-fade-up">
        <p className="eyebrow">Pipeline</p>
        <h1 className="mt-3 font-display text-4xl leading-none tracking-tightest text-white">
          Where every application stands
        </h1>
      </header>

      <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>

      <AutopilotBar onError={setError} toast={toast} />

      <StatBar stats={stats} />

      {campaigns.length > 0 && (
        <CampaignStrip
          campaigns={campaigns}
          filter={filters.campaign_id}
          onFilter={(campaign_id) => setFilters((f) => ({ ...f, campaign_id }))}
          onChange={load}
          onError={setError}
          toast={toast}
        />
      )}

      {stats.total_applications === 0 ? (
        <EmptyState filtered={hasFilter} onClear={() => setFilters(EMPTY_FILTERS)} />
      ) : (
        <>
          <Funnel stages={pipeline} total={stats.total_applications} />

          <FilterBar
            filters={filters}
            options={options}
            onChange={setFilters}
            onClear={() => setFilters(EMPTY_FILTERS)}
            onExport={() => exportCsv(filters, toast, setError)}
          />

          <ApplicationTable rows={applications} />

          <Analytics filters={filters} onError={setError} />

          <div className="grid gap-4 lg:grid-cols-[1.6fr_1fr]">
            <ActivityFeed events={activity} />
            <UpcomingQueue queue={queue} />
          </div>
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/**
 * Download the table as CSV. The server renders it from the same read the page
 * did, filters and all, so the file always matches what was on screen.
 */
async function exportCsv(filters, toast, onError) {
  try {
    const text = await api.exportApplicationsCsv(filters);
    const href = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
    const link = document.createElement("a");
    link.href = href;
    link.download = `doaide-autoapply-pipeline-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(href);
    toast.success("Downloaded.");
  } catch (err) {
    onError(err.message);
    toast.error(err.message);
  }
}

/* -------------------------------------------------------------------------- */

/**
 * Autopilot's switch, its two live knobs, and Run now — everything the old
 * Autopilot page was actually used for, in one strip above the funnel it feeds.
 *
 * The two knobs here (selectivity, daily volume) are the ones a user reaches
 * for while *watching* results: too few applications, or too many of the wrong
 * kind. Targeting, follow-up cadence and the rest stay in setup, because they
 * are set once and not adjusted against a funnel.
 */
function AutopilotBar({ onError, toast }) {
  const [pref, setPref] = useState(null);
  const [reputation, setReputation] = useState([]);
  const [onboarding, setOnboarding] = useState(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);

  const refresh = useCallback(async () => {
    const [p, rep, onboard] = await Promise.all([
      api.getAutopilot(),
      api.autopilotReputation().catch(() => []),
      api.onboarding().catch(() => null),
    ]);
    setPref(p);
    setReputation(rep);
    setOnboarding(onboard);
  }, []);

  useEffect(() => {
    // A failing read here must not take the pipeline down with it — the strip
    // simply doesn't render.
    refresh().catch(() => {});
  }, [refresh]);

  if (!pref) return null;

  async function save(changes) {
    setSaving(true);
    // Optimistic: reflect the change immediately, reconcile with the server.
    setPref((prev) => ({ ...prev, ...changes }));
    try {
      setPref(await api.updateAutopilot(changes));
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
      await refresh().catch(() => {});
    } finally {
      setSaving(false);
    }
  }

  async function runNow() {
    setRunning(true);
    try {
      const result = await api.runAutopilot();
      if (result.queued) {
        toast.info("Autopilot is running in the background — check back shortly.");
      } else {
        toast.success(
          `Applied to ${result.applied} of ${result.scanned} matches (budget ${result.budget}).`,
        );
      }
      await refresh();
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setRunning(false);
    }
  }

  const blocked = runDisabledReason({ onboarding, pref, running });

  return (
    <section className="panel space-y-4 p-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <ActiveToggle
          active={pref.is_active}
          busy={saving}
          onToggle={() => save({ is_active: !pref.is_active })}
        />

        <div className="flex flex-wrap items-end gap-4">
          <NumberKnob
            id="ap-fit"
            label="Min fit"
            suffix="%"
            value={pref.min_fit_score}
            min={0}
            max={100}
            onCommit={(value) => save({ min_fit_score: value })}
          />
          <NumberKnob
            id="ap-limit"
            label="Per day"
            value={pref.daily_application_limit}
            min={1}
            max={50}
            onCommit={(value) => save({ daily_application_limit: value })}
          />
          <div className="text-right">
            <p className="eyebrow">Sent by autopilot</p>
            <p className="mt-1 font-display text-2xl tracking-tight text-white">
              {pref.applications_created}
            </p>
          </div>
          <Tooltip label={blocked}>
            <button className="btn-ghost" onClick={runNow} disabled={running || Boolean(blocked)}>
              {running ? "Running…" : "Run now"}
            </button>
          </Tooltip>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-3 hairline">
        <p className="font-mono text-[11px] text-white/30">
          Last run {pref.last_run_at ? formatWhen(pref.last_run_at) : "never"}
          {reputation.length > 0 && (
            <>
              {" · "}
              {reputation
                .map(
                  (account) =>
                    `${account.email} ${account.paused ? "paused" : `${account.day_limit}/day`}`,
                )
                .join(" · ")}
            </>
          )}
        </p>
        <Link to="/setup" className="btn-quiet">
          All settings
        </Link>
      </div>

      {pref.last_error && (
        <p className="text-xs text-warn">{pref.last_error}</p>
      )}
    </section>
  );
}

/** Why "Run now" is disabled, or null when it's runnable. */
function runDisabledReason({ onboarding, pref, running }) {
  if (running) return null; // button already reads "Running…"
  if (onboarding && !onboarding.gmail_connected) return "Connect Gmail first";
  if (onboarding && onboarding.resume_count === 0) return "Upload a resume first";
  if (!pref.is_active) return "Turn autopilot on to run it";
  return null;
}

function ActiveToggle({ active, onToggle, busy }) {
  return (
    <button
      onClick={onToggle}
      disabled={busy}
      aria-pressed={active}
      className={[
        "flex items-center gap-3 rounded-xl border px-4 py-2.5 transition-colors",
        active
          ? "border-good/40 bg-good/[0.08]"
          : "border-white/10 bg-white/[0.02] hover:border-white/20",
      ].join(" ")}
    >
      <span
        className={[
          "relative h-6 w-11 rounded-full transition-colors",
          active ? "bg-good" : "bg-white/15",
        ].join(" ")}
      >
        <span
          className={[
            "absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all",
            active ? "left-[22px]" : "left-0.5",
          ].join(" ")}
        />
      </span>
      <span className="text-left">
        <span className="block text-sm font-medium text-white">
          {active ? "Autopilot on" : "Autopilot off"}
        </span>
        <span className="block font-mono text-[10px] uppercase tracking-wider text-white/40">
          {active ? "running" : "paused"}
        </span>
      </span>
    </button>
  );
}

/**
 * A small number field that only writes on blur or Enter. Saving per keystroke
 * would fire a PUT for "7" on the way to "70" — and briefly narrow the user's
 * targeting to something they never asked for.
 */
function NumberKnob({ id, label, value, min, max, suffix = "", onCommit }) {
  const [draft, setDraft] = useState(String(value));

  useEffect(() => {
    setDraft(String(value));
  }, [value]);

  function commit() {
    const parsed = Number(draft);
    if (!Number.isFinite(parsed)) {
      setDraft(String(value));
      return;
    }
    const clamped = Math.min(Math.max(Math.round(parsed), min), max);
    setDraft(String(clamped));
    if (clamped !== value) onCommit(clamped);
  }

  return (
    <div className="w-24">
      <label className="label" htmlFor={id}>
        {label}
        {suffix}
      </label>
      <input
        id={id}
        type="number"
        className="input"
        min={min}
        max={max}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
      />
    </div>
  );
}

function StatBar({ stats }) {
  const items = [
    { label: "Applications", value: stats.total_applications },
    { label: "In flight", value: stats.active },
    {
      label: "Response rate",
      value: `${Math.round((stats.response_rate || 0) * 100)}%`,
      accent: true,
    },
    {
      label: "Interview rate",
      value: `${Math.round((stats.interview_rate || 0) * 100)}%`,
    },
    {
      label: "Avg fit",
      value: stats.average_fit_score !== null ? Math.round(stats.average_fit_score) : "—",
    },
  ];

  return (
    <div className="stagger grid grid-cols-2 gap-px overflow-hidden rounded-xl border bg-white/[0.06] hairline sm:grid-cols-3 lg:grid-cols-5">
      {items.map((item) => (
        <div key={item.label} className="bg-ink-800 px-5 py-5">
          <p className={["stat-figure", item.accent ? "text-signal" : ""].join(" ")}>
            {item.value}
          </p>
          <p className="eyebrow mt-2">{item.label}</p>
        </div>
      ))}
    </div>
  );
}

function CampaignStrip({ campaigns, filter, onFilter, onChange, onError, toast }) {
  async function toggle(campaign) {
    try {
      if (campaign.status === "PAUSED") await api.resumeCampaign(campaign.id);
      else await api.pauseCampaign(campaign.id);
      await onChange();
      toast.success(
        campaign.status === "PAUSED" ? "Campaign resumed." : "Campaign paused.",
      );
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    }
  }

  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="eyebrow">Campaigns</p>
        {filter && (
          <button className="btn-quiet" onClick={() => onFilter("")}>
            Clear filter
          </button>
        )}
      </div>

      <ul className="space-y-2">
        {campaigns.map((campaign) => {
          const live = LIVE_CAMPAIGN_STATUSES.has(campaign.status);
          const selected = String(filter) === String(campaign.id);
          return (
            <li
              key={campaign.id}
              className={[
                "panel flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 transition-colors",
                selected ? "border-signal/40" : "",
              ].join(" ")}
            >
              <button
                className="min-w-0 flex-1 truncate text-left text-sm text-white/85 hover:text-white"
                onClick={() => onFilter(selected ? "" : campaign.id)}
                title="Filter the table to this campaign"
              >
                {campaign.name}
              </button>

              <span className="font-mono text-[11px] text-white/35">
                {campaign.contacts_found} contacts · {campaign.emails_generated} emails
              </span>

              <span
                className={[
                  "badge",
                  live
                    ? "bg-signal/15 text-signal"
                    : campaign.status === "PAUSED"
                      ? "bg-warn/15 text-warn"
                      : campaign.status === "FAILED"
                        ? "bg-bad/15 text-bad"
                        : "bg-white/[0.06] text-white/45",
                ].join(" ")}
              >
                {live && (
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-signal" />
                )}
                {campaign.status.toLowerCase()}
              </span>

              {campaign.status !== "COMPLETED" && (
                <button className="btn-quiet" onClick={() => toggle(campaign)}>
                  {campaign.status === "PAUSED" ? "Resume" : "Pause"}
                </button>
              )}

              {campaign.last_error && (
                <p className="w-full font-mono text-[11px] text-white/30">
                  {campaign.last_error}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/**
 * The funnel. Bar width is the share of applications that reached each stage,
 * so the shape itself is the insight — a cliff between two columns is where the
 * process is leaking.
 */
function Funnel({ stages, total }) {
  return (
    <section className="panel space-y-4 p-5">
      <div className="flex items-baseline justify-between">
        <p className="eyebrow">Pipeline</p>
        <p className="font-mono text-[11px] text-white/30">{total} total · cumulative</p>
      </div>

      <ul className="space-y-3">
        {stages.map((stage, index) => (
          <li key={stage.key}>
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-sm text-white/75">{stage.label}</span>
              <span className="font-mono text-xs text-white/45">
                {stage.count}
                <span className="ml-2 text-white/25">
                  {Math.round((stage.rate || 0) * 100)}%
                </span>
              </span>
            </div>
            <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-white/[0.05]">
              <div
                className="h-full rounded-full transition-all duration-700"
                style={{
                  width: `${Math.max(stage.rate * 100, stage.count ? 2 : 0)}%`,
                  background: STAGE_TONE[index] ?? "#f0b429",
                  opacity: stage.count ? 1 : 0.3,
                }}
              />
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function FilterBar({ filters, options, onChange, onClear, onExport }) {
  const set = (key) => (e) => onChange({ ...filters, [key]: e.target.value });
  const active = Object.values(filters).some(Boolean);

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="w-44">
        <label className="label" htmlFor="d-company">
          Company
        </label>
        <select id="d-company" className="input" value={filters.company} onChange={set("company")}>
          <option value="" className="bg-ink-800">
            All
          </option>
          {options.companies.map((name) => (
            <option key={name} value={name} className="bg-ink-800">
              {name}
            </option>
          ))}
        </select>
      </div>

      <div className="w-48">
        <label className="label" htmlFor="d-campaign">
          Campaign
        </label>
        <select
          id="d-campaign"
          className="input"
          value={filters.campaign_id}
          onChange={set("campaign_id")}
        >
          <option value="" className="bg-ink-800">
            All
          </option>
          {options.campaigns.map((campaign) => (
            <option key={campaign.id} value={campaign.id} className="bg-ink-800">
              {campaign.name}
            </option>
          ))}
        </select>
      </div>

      <div className="w-44">
        <label className="label" htmlFor="d-status">
          Status
        </label>
        <select id="d-status" className="input" value={filters.status} onChange={set("status")}>
          <option value="" className="bg-ink-800">
            All
          </option>
          {options.statuses.map((status) => (
            <option key={status} value={status} className="bg-ink-800">
              {status.toLowerCase().replace(/_/g, " ")}
            </option>
          ))}
        </select>
      </div>

      <div className="w-36">
        <label className="label" htmlFor="d-days">
          Period
        </label>
        <select id="d-days" className="input" value={filters.days} onChange={set("days")}>
          <option value="" className="bg-ink-800">
            All time
          </option>
          <option value="7" className="bg-ink-800">
            Last 7 days
          </option>
          <option value="30" className="bg-ink-800">
            Last 30 days
          </option>
          <option value="90" className="bg-ink-800">
            Last 90 days
          </option>
        </select>
      </div>

      {active && (
        <button className="btn-quiet mb-1" onClick={onClear}>
          Clear
        </button>
      )}

      <button className="btn-quiet mb-1 ml-auto" onClick={onExport}>
        Export CSV
      </button>
    </div>
  );
}

function ApplicationTable({ rows }) {
  if (!rows.length) {
    return (
      <div className="panel px-6 py-10 text-center text-sm text-white/35">
        Nothing matches those filters.
      </div>
    );
  }

  return (
    <div className="panel overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] border-collapse text-left">
          <thead>
            <tr className="border-b hairline">
              {["Company", "Role", "Stage", "Contact", "Next follow-up", "Last activity"].map(
                (heading) => (
                  <th key={heading} className="cell eyebrow font-normal">
                    {heading}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.application_id}
                className="border-b transition-colors last:border-0 hover:bg-white/[0.02] hairline"
              >
                <td className="cell text-sm text-white/85">{row.company || "—"}</td>
                <td className="cell text-xs text-white/50">{row.role || "—"}</td>
                <td className="cell">
                  <span className="badge bg-white/[0.06] text-white/60">
                    {row.status.toLowerCase().replace(/_/g, " ")}
                  </span>
                </td>
                <td className="cell font-mono text-xs text-white/40">{row.contact || "—"}</td>
                <td className="cell whitespace-nowrap font-mono text-xs text-white/40">
                  {row.next_follow_up_at ? formatWhen(row.next_follow_up_at) : "—"}
                </td>
                <td className="cell whitespace-nowrap font-mono text-xs text-white/30">
                  {formatWhen(row.last_activity_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Analytics                                                                  */
/* -------------------------------------------------------------------------- */

/**
 * What is actually working — the counterpart to the funnel above, which only
 * says where things are. Four questions: is the response rate moving, where do
 * applications pile up, which resume performs, and who writes back.
 *
 * Collapsed by default. It is a second read and a second scroll, and the page's
 * job on open is the pipeline.
 */
function Analytics({ filters, onError }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [bucket, setBucket] = useState("week");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api
      .analytics({ days: filters.days, bucket })
      .then((result) => !cancelled && setData(result))
      .catch((err) => !cancelled && onError(err.message));
    return () => {
      cancelled = true;
    };
  }, [open, bucket, filters.days, onError]);

  return (
    <section className="panel overflow-hidden">
      <button
        className="flex w-full items-center justify-between gap-3 px-5 py-4 text-left transition-colors hover:bg-white/[0.02]"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span>
          <span className="eyebrow block">Analytics</span>
          <span className="mt-1 block text-sm text-white/45">
            Response rate over time, and what's producing it
          </span>
        </span>
        <span className="font-mono text-xs text-white/30">{open ? "Hide" : "Show"}</span>
      </button>

      {open &&
        (!data ? (
          <div className="px-5 pb-5">
            <SkeletonLoader rows={[180]} />
          </div>
        ) : data.total_applications === 0 ? (
          <p className="px-5 pb-6 text-sm text-white/35">
            Nothing to analyse yet — send a few applications first.
          </p>
        ) : (
          <div className="space-y-8 border-t px-5 py-6 hairline">
            <AnalyticsHeadline data={data} bucket={bucket} onBucket={setBucket} />
            <EngagementRow engagement={data.engagement} />
            <ResponseTrend points={data.trend} />
            <StatusBreakdown slices={data.by_status} />
            <SubjectExperiments onError={onError} />
            <ResumeTable rows={data.resumes} minSample={data.min_sample} />
            <div className="grid gap-6 sm:grid-cols-2">
              <SegmentList
                title="Most responsive companies"
                rows={data.companies}
                minSample={data.min_sample}
              />
              <SegmentList
                title="Most responsive industries"
                rows={data.industries}
                minSample={data.min_sample}
              />
            </div>
          </div>
        ))}
    </section>
  );
}

function pct(rate) {
  return `${Math.round((rate || 0) * 100)}%`;
}

function AnalyticsHeadline({ data, bucket, onBucket }) {
  const items = [
    { label: "Applications", value: data.total_applications },
    { label: "Responses", value: data.responses },
    { label: "Response rate", value: pct(data.response_rate), accent: true },
    { label: "Interview rate", value: pct(data.interview_rate) },
    {
      label: "Median days to reply",
      value: data.median_days_to_reply ?? "—",
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="eyebrow">Overall</p>
        <div className="flex gap-1">
          {["week", "month"].map((key) => (
            <button
              key={key}
              onClick={() => onBucket(key)}
              className={[
                "rounded-md px-2.5 py-1 text-xs transition-colors",
                bucket === key
                  ? "bg-white/[0.07] text-white"
                  : "text-white/35 hover:text-white/70",
              ].join(" ")}
            >
              By {key}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-x-10 gap-y-4">
        {items.map((item) => (
          <div key={item.label}>
            <p className={["stat-figure", item.accent ? "text-signal" : ""].join(" ")}>
              {item.value}
            </p>
            <p className="eyebrow mt-1.5">{item.label}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Opens and clicks. A 0% reply rate with a 60% open rate and one with a 0% open
 * rate are completely different problems — the first is a weak pitch, the second
 * a weak subject line or a deliverability issue — and until this row existed the
 * product could not tell the user which one they had.
 *
 * Captioned "approximate" deliberately, and not softly: mail proxies fetch the
 * pixel at delivery whether or not anyone reads the message, and corporate
 * gateways block images entirely. The number is directional. Presenting it as
 * a measurement would be a lie the user would then act on.
 */
function EngagementRow({ engagement }) {
  if (!engagement?.tracked) return null;

  const items = [
    { label: "Tracked sends", value: engagement.tracked },
    { label: "Opened", value: pct(engagement.open_rate), accent: true },
    { label: "Clicked", value: pct(engagement.click_rate) },
    { label: "Clicked after opening", value: pct(engagement.click_to_open_rate) },
  ];

  return (
    <div className="space-y-3 border-t pt-6 hairline">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="eyebrow">Engagement</p>
        <p className="font-mono text-[11px] text-white/30">
          approximate — image blocking and mail-proxy prefetch both distort opens
        </p>
      </div>
      <div className="flex flex-wrap gap-x-10 gap-y-4">
        {items.map((item) => (
          <div key={item.label}>
            <p className={["stat-figure", item.accent ? "text-signal" : ""].join(" ")}>
              {item.value}
            </p>
            <p className="eyebrow mt-1.5">{item.label}</p>
          </div>
        ))}
      </div>
      {!engagement.reliable && (
        <p className="text-xs text-white/30">
          Too few tracked sends to read anything into these yet.
        </p>
      )}
    </div>
  );
}

/**
 * Which subject line is getting opened, per campaign.
 *
 * A campaign is tens of emails, not thousands, so nothing here is presented as
 * significant until every arm has enough impressions — the server decides that
 * and sends `confident`, rather than the UI inventing its own threshold.
 */
function SubjectExperiments({ onError }) {
  const [experiments, setExperiments] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api
      .subjectVariants()
      .then((rows) => !cancelled && setExperiments(rows))
      .catch((err) => !cancelled && onError(err.message));
    return () => {
      cancelled = true;
    };
  }, [onError]);

  if (!experiments?.length) return null;

  return (
    <div className="space-y-4 border-t pt-6 hairline">
      <p className="eyebrow">Subject lines</p>
      {experiments.map((experiment) => (
        <div key={experiment.campaign_id} className="space-y-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-sm text-white/70">{experiment.campaign_name}</p>
            {!experiment.confident && (
              <p className="font-mono text-[11px] text-white/30">
                early — not enough data yet
              </p>
            )}
          </div>
          <ul className="space-y-2">
            {experiment.variants.map((variant) => (
              <li key={variant.id} className="flex items-baseline gap-3">
                <span className="shrink-0 font-mono text-[11px] text-white/30">
                  {variant.label}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm text-white/70">
                  {variant.text}
                  {variant.is_winner && (
                    <span className="ml-2 text-signal" title="Winning subject line">
                      ★
                    </span>
                  )}
                </span>
                <span className="shrink-0 font-mono text-[11px] text-white/30">
                  {variant.sends} sent
                </span>
                <span className="shrink-0 font-mono text-sm text-white/60">
                  {pct(variant.open_rate)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

/**
 * Response rate per cohort, as columns. Bar height is the rate; the number
 * under it is the volume, because a 100% week made of one application should
 * not read like the best week on record.
 */
function ResponseTrend({ points }) {
  if (!points?.length) return null;
  const peak = Math.max(...points.map((p) => p.response_rate), 0.01);

  return (
    <div className="space-y-3 border-t pt-6 hairline">
      <div className="flex items-baseline justify-between">
        <p className="eyebrow">Response rate over time</p>
        <p className="font-mono text-[11px] text-white/30">by when you applied</p>
      </div>
      <ul className="flex items-end gap-2 overflow-x-auto pb-1">
        {points.map((point) => (
          <li key={point.label} className="flex min-w-[3.25rem] flex-1 flex-col items-center gap-2">
            <span className="font-mono text-[10px] text-white/45">
              {pct(point.response_rate)}
            </span>
            <div
              className="flex h-24 w-full items-end rounded-md bg-white/[0.04]"
              title={`${point.responses}/${point.applications} replied`}
            >
              <div
                className="w-full rounded-md bg-signal/70 transition-all duration-700"
                style={{
                  height: `${Math.max((point.response_rate / peak) * 100, point.applications ? 4 : 0)}%`,
                }}
              />
            </div>
            <span className="whitespace-nowrap font-mono text-[10px] text-white/30">
              {point.label}
            </span>
            <span className="font-mono text-[10px] text-white/20">
              n={point.applications}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function StatusBreakdown({ slices }) {
  if (!slices?.length) return null;
  return (
    <div className="space-y-3 border-t pt-6 hairline">
      <p className="eyebrow">Applications by status</p>
      <ul className="space-y-2">
        {slices.map((slice) => (
          <li key={slice.status}>
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-sm text-white/75">{slice.label}</span>
              <span className="font-mono text-xs text-white/45">
                {slice.count}
                <span className="ml-2 text-white/25">{pct(slice.share)}</span>
              </span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-white/[0.05]">
              <div
                className="h-full rounded-full bg-white/25 transition-all duration-700"
                style={{ width: `${Math.max(slice.share * 100, 2)}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ResumeTable({ rows, minSample }) {
  if (!rows?.length) return null;
  return (
    <div className="space-y-3 border-t pt-6 hairline">
      <div className="flex items-baseline justify-between gap-3">
        <p className="eyebrow">Resume performance</p>
        <p className="font-mono text-[11px] text-white/30">
          {minSample}+ applications to rank
        </p>
      </div>
      <ul className="space-y-2">
        {rows.map((row) => (
          <li
            key={row.resume_id ?? "none"}
            className={[
              "flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border px-4 py-3 hairline",
              row.is_best ? "border-good/30 bg-good/[0.05]" : "bg-ink-900/40",
            ].join(" ")}
          >
            <span className="min-w-0 flex-1 truncate text-sm text-white/80">
              {row.label}
            </span>
            {row.is_best && <span className="badge bg-good/15 text-good">best</span>}
            <span className="font-mono text-[11px] text-white/35">
              {row.applications} sent · {row.responses} replied · {row.interviews} interviews
            </span>
            <span className="font-mono text-sm text-white/70">{pct(row.response_rate)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SegmentList({ title, rows, minSample }) {
  return (
    <div className="space-y-3">
      <p className="eyebrow">{title}</p>
      {!rows?.length ? (
        <p className="text-xs text-white/30">Not enough data yet.</p>
      ) : (
        <ul className="space-y-2">
          {rows.map((row) => (
            <li key={row.name} className="flex items-baseline gap-3">
              <span className="min-w-0 flex-1 truncate text-sm text-white/70">
                {row.name}
              </span>
              <span className="shrink-0 font-mono text-[11px] text-white/30">
                {row.responses}/{row.applications}
                {row.applications < minSample && (
                  // Flagged rather than hidden: the user should see the row
                  // exists without reading its percentage as a finding.
                  <span className="ml-1 text-white/20" title="Too few to be meaningful">
                    ·thin
                  </span>
                )}
              </span>
              <span className="shrink-0 font-mono text-sm text-white/60">
                {pct(row.response_rate)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function ActivityFeed({ events }) {
  return (
    <section className="panel p-5">
      <p className="eyebrow">Activity</p>
      {events.length === 0 ? (
        <p className="mt-4 text-sm text-white/30">Nothing has happened yet.</p>
      ) : (
        <ol className="mt-4 space-y-3">
          {events.map((event, index) => {
            const [label, tone] = EVENT_STYLE[event.kind] ?? [event.kind, "text-white/40"];
            return (
              <li key={`${event.at}-${index}`} className="flex gap-3">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-white/20" />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className={`font-mono text-[10px] uppercase tracking-wider ${tone}`}>
                      {label}
                    </span>
                    <span className="truncate text-sm text-white/70">
                      {event.company || event.contact || "—"}
                    </span>
                    <span className="ml-auto shrink-0 font-mono text-[10px] text-white/25">
                      {formatWhen(event.at)}
                    </span>
                  </div>
                  {event.summary && (
                    <p className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-white/35">
                      {event.summary}
                    </p>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function UpcomingQueue({ queue }) {
  return (
    <section className="panel space-y-4 p-5">
      <p className="eyebrow">Up next</p>

      <div className="flex items-baseline gap-2">
        <span className="font-display text-3xl leading-none tracking-tightest text-white">
          {queue?.pending_sends ?? 0}
        </span>
        <span className="text-xs text-white/35">emails queued to send</span>
      </div>

      {queue?.follow_ups?.length ? (
        <ul className="space-y-2 border-t pt-4 hairline">
          {queue.follow_ups.slice(0, 6).map((item) => (
            <li key={item.id} className="flex items-baseline justify-between gap-3">
              <span className="truncate text-xs text-white/55">
                Follow-up {item.step}
                <span className="ml-1.5 text-white/25">
                  {item.template.toLowerCase().replace(/_/g, " ")}
                </span>
              </span>
              <span className="shrink-0 font-mono text-[10px] text-white/30">
                {formatWhen(item.scheduled_at)}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="border-t pt-4 text-xs text-white/30 hairline">
          No follow-ups scheduled.
        </p>
      )}
    </section>
  );
}

function EmptyState({ filtered, onClear }) {
  return (
    <div className="panel px-6 py-16 text-center">
      <p className="font-display text-2xl tracking-tight text-white/80">
        {filtered ? "Nothing matches those filters" : "No applications yet"}
      </p>
      <p className="mx-auto mt-2 max-w-sm text-sm text-white/40">
        {filtered
          ? "Widen the filters to see the rest of your pipeline."
          : "Start a campaign and every recruiter it reaches shows up here, with its stage and history."}
      </p>
      {filtered ? (
        <button className="btn-ghost mt-6" onClick={onClear}>
          Clear filters
        </button>
      ) : (
        <Link to="/setup" className="btn-ghost mt-6">
          Go to setup
        </Link>
      )}
    </div>
  );
}
