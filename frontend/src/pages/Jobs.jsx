import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { FitBreakdown, FitPill, FitRing } from "../components/FitScore";
import ApplyActions from "../components/ApplyActions";
import JobIntel from "../components/JobIntel";
import CopyButton from "../components/ui/CopyButton";
import { useConfirm } from "../components/ui/ConfirmDialog";
import ErrorBanner from "../components/ui/ErrorBanner";
import ShortcutHints from "../components/ui/ShortcutHints";
import { useToast } from "../components/ui/Toast";
import { useDebouncedValue } from "../hooks/useDebounce";
import { useKeyboard } from "../hooks/useKeyboard";
import { api } from "../lib/api";

const STATUS_STYLE = {
  NEW: ["new", "bg-signal/15 text-signal"],
  SAVED: ["saved", "bg-sky-400/15 text-sky-300"],
  TAILORED: ["tailored", "bg-good/15 text-good"],
  APPLIED: ["applied", "bg-good/20 text-good"],
  DISMISSED: ["dismissed", "bg-white/[0.06] text-white/30"],
};

const JOB_SHORTCUTS = [
  ["j / k", "move"],
  ["enter", "open"],
  ["x", "select"],
  ["esc", "close"],
];

/**
 * The job feed — what the monitoring agent found, best fit first.
 *
 * Ordering by fit rather than recency is the whole argument of the product: the
 * point isn't to see every job, it's to see the handful worth an application.
 *
 * Two things make a long scan tolerable: triage in bulk (a 40-result sweep is
 * mostly "no", and doing that one row at a time is where people give up), and
 * tailoring without leaving the page — the drawer opens against the job you're
 * looking at, so choosing and acting stay in one place.
 */
export default function Jobs() {
  const toast = useToast();
  const confirm = useConfirm();
  const [jobs, setJobs] = useState([]);
  const [searches, setSearches] = useState([]);
  const [providers, setProviders] = useState(null);
  // Only used to decide whether the per-job profile badge is worth showing:
  // with one profile, naming it on every card says nothing.
  const [profileCount, setProfileCount] = useState(0);
  const [filters, setFilters] = useState({ company: "", min_fit: "", status: "" });
  const [error, setError] = useState(null);
  const [scanning, setScanning] = useState(null);
  // Ids ticked for a bulk action, and the job whose drawer is open.
  const [selected, setSelected] = useState(() => new Set());
  const [openJobId, setOpenJobId] = useState(null);
  // The keyboard cursor, which is separate from both: moving it neither selects
  // nor opens anything until you press for it.
  const [cursorId, setCursorId] = useState(null);
  const [busy, setBusy] = useState(false);

  // Debounce the filter so a text/number keystroke doesn't fire a request each
  // tick — the feed only refetches once typing settles.
  const debouncedFilters = useDebouncedValue(filters, 300);

  const load = useCallback(async () => {
    const [feed, searchList] = await Promise.all([
      api.listJobs({
        company: debouncedFilters.company,
        min_fit: debouncedFilters.min_fit,
        status: debouncedFilters.status,
      }),
      api.listSearches(),
    ]);
    setJobs(feed);
    setSearches(searchList);
    // Anything that has left the feed can't be acted on, so drop it from the
    // selection rather than sending ids the server will report as not found.
    const live = new Set(feed.map((job) => job.id));
    setSelected((prev) => new Set([...prev].filter((id) => live.has(id))));
  }, [debouncedFilters]);

  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, [load]);

  useEffect(() => {
    api.jobProviders().then(setProviders).catch(() => {});
    api
      .listProfiles()
      .then((list) => setProfileCount(list.length))
      .catch(() => {});
  }, []);

  const openJob = useMemo(
    () => jobs.find((job) => job.id === openJobId) ?? null,
    [jobs, openJobId],
  );

  const move = useCallback(
    (delta) => {
      if (!jobs.length) return;
      const current = jobs.findIndex((job) => job.id === cursorId);
      const next =
        current === -1
          ? delta > 0
            ? 0
            : jobs.length - 1
          : Math.min(Math.max(current + delta, 0), jobs.length - 1);
      setCursorId(jobs[next].id);
    },
    [jobs, cursorId],
  );

  useKeyboard(
    {
      j: () => move(1),
      k: () => move(-1),
      Enter: () => (cursorId != null ? setOpenJobId(cursorId) : move(1)),
      x: () => cursorId != null && toggle(cursorId),
      Escape: () => {
        // Escape unwinds one layer at a time: the drawer first, then the
        // selection. Clearing both at once loses work the user didn't ask to
        // lose.
        if (openJobId != null) setOpenJobId(null);
        else if (selected.size) setSelected(new Set());
      },
    },
    { enabled: jobs.length > 0 },
  );

  function toggle(id) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function runSearch(id) {
    setError(null);
    setScanning(id);
    try {
      const result = await api.runSearch(id);
      toast.success(
        result.added > 0
          ? `Found ${result.added} new ${result.added === 1 ? "job" : "jobs"}.`
          : `No new jobs — ${result.duplicates} already seen${
              result.below_threshold
                ? `, ${result.below_threshold} below your fit threshold`
                : ""
            }.`,
      );
      await load();
    } catch (err) {
      setError(err.message);
      toast.error(err.message);
    } finally {
      setScanning(null);
    }
  }

  async function triage(id, status) {
    try {
      await api.patchJob(id, { status });
      await load();
    } catch (err) {
      setError(err.message);
      toast.error(err.message);
    }
  }

  async function bulk(action) {
    const ids = [...selected];
    if (!ids.length) return;

    if (
      action === "archive" &&
      !(await confirm({
        title: `Archive ${ids.length} ${ids.length === 1 ? "job" : "jobs"}?`,
        message:
          "They'll be removed from your feed for good. Dismissing instead just hides them, and keeps them findable.",
        confirmLabel: "Archive",
        tone: "danger",
      }))
    )
      return;

    setError(null);
    setBusy(true);
    try {
      const result = await api.bulkJobs(ids, action);
      const changed = result.deleted || result.updated;
      toast.success(
        changed === 0
          ? "Nothing to change — those were already there."
          : `${changed} ${changed === 1 ? "job" : "jobs"} ${PAST_TENSE[action]}.` +
              (result.skipped ? ` ${result.skipped} already were.` : ""),
      );
      setSelected(new Set());
      await load();
    } catch (err) {
      setError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-10">
      <header className="animate-fade-up">
        <p className="eyebrow">Job feed</p>
        <h1 className="mt-3 font-display text-4xl leading-none tracking-tightest text-white">
          Roles worth your time
        </h1>
        <p className="mt-3 max-w-xl text-sm leading-relaxed text-white/45">
          Every job is scored against your resume before it reaches this page.
          Anything below your threshold never shows up.
        </p>
      </header>

      <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>

      <SearchManager
        searches={searches}
        scanning={scanning}
        providers={providers}
        onRun={runSearch}
        onChange={load}
        onError={setError}
        toast={toast}
      />

      {jobs.length > 0 && <Filters filters={filters} onChange={setFilters} />}

      {jobs.length === 0 ? (
        <EmptyFeed hasSearch={searches.length > 0} />
      ) : (
        <div className="space-y-3">
          <SelectionBar
            jobs={jobs}
            selected={selected}
            busy={busy}
            onSelectAll={() => setSelected(new Set(jobs.map((job) => job.id)))}
            onClear={() => setSelected(new Set())}
            onAction={bulk}
          />

          <ul className="stagger space-y-2">
            {jobs.map((job) => (
              <JobCard
                key={job.id}
                job={job}
                checked={selected.has(job.id)}
                focused={job.id === cursorId}
                showProfile={profileCount > 1}
                onToggle={() => toggle(job.id)}
                onTriage={triage}
                onOpen={() => setOpenJobId(job.id)}
                onError={setError}
              />
            ))}
          </ul>

          <ShortcutHints hints={JOB_SHORTCUTS} />
        </div>
      )}

      {openJob && (
        <TailorDrawer
          job={openJob}
          onClose={() => setOpenJobId(null)}
          onTriage={triage}
          onError={setError}
          toast={toast}
        />
      )}
    </div>
  );
}

const PAST_TENSE = {
  save: "saved",
  apply: "marked applied",
  dismiss: "dismissed",
  archive: "archived",
};

/* -------------------------------------------------------------------------- */

/**
 * The bulk bar. Always present once there are jobs — appearing only on the
 * first tick hides the fact that bulk triage exists at all, which is the
 * feature's whole problem.
 */
function SelectionBar({ jobs, selected, busy, onSelectAll, onClear, onAction }) {
  const count = selected.size;
  const all = count > 0 && count === jobs.length;

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-ink-900/40 px-4 py-2.5 hairline">
      <label className="flex cursor-pointer items-center gap-2.5 text-sm">
        <input
          type="checkbox"
          className="h-4 w-4 accent-signal"
          checked={all}
          // Some ticked but not all: the box shows a dash, which is what a
          // "select all" control should say when the answer is "some".
          ref={(el) => el && (el.indeterminate = count > 0 && !all)}
          onChange={() => (all || count > 0 ? onClear() : onSelectAll())}
          aria-label={all ? "Clear selection" : "Select all jobs"}
        />
        <span className={count ? "text-white/80" : "text-white/40"}>
          {count ? `${count} selected` : "Select"}
        </span>
      </label>

      {count > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <button className="btn-quiet" onClick={() => onAction("save")} disabled={busy}>
            Save
          </button>
          <button className="btn-quiet" onClick={() => onAction("apply")} disabled={busy}>
            Mark applied
          </button>
          <button className="btn-quiet" onClick={() => onAction("dismiss")} disabled={busy}>
            Dismiss
          </button>
          <button
            className="btn-quiet hover:text-bad"
            onClick={() => onAction("archive")}
            disabled={busy}
          >
            Archive
          </button>
        </div>
      )}
    </div>
  );
}

function JobCard({
  job,
  checked,
  focused,
  showProfile,
  onToggle,
  onTriage,
  onOpen,
  onError,
}) {
  const [label, tone] = STATUS_STYLE[job.status] ?? [job.status.toLowerCase(), "bg-white/5"];
  // Intel is fetched on expand, not with the feed: it costs a salary lookup and
  // a company research call, and most rows on a hundred-job page never open.
  const [expanded, setExpanded] = useState(false);

  return (
    <li
      className={[
        "panel transition-colors",
        focused ? "border-signal/40 bg-white/[0.03]" : "hover:border-white/[0.14]",
      ].join(" ")}
      aria-current={focused ? "true" : undefined}
    >
      <div className="flex flex-wrap items-start gap-x-4 gap-y-3 px-4 py-4">
        <input
          type="checkbox"
          className="mt-1 h-4 w-4 shrink-0 accent-signal"
          checked={checked}
          onChange={onToggle}
          aria-label={`Select ${job.title || "this job"}`}
        />

        <button className="min-w-0 flex-1 text-left" onClick={onOpen}>
          <div className="flex flex-wrap items-center gap-2">
            <FitPill score={job.fit_score} />
            <span className="truncate text-sm font-medium text-white">
              {job.title || "Untitled role"}
            </span>
            <span className={`badge ${tone}`}>{label}</span>
            {/* Scout's number only earns a badge when it disagrees with the
                deterministic one — two identical scores side by side is noise. */}
            {job.llm_fit_score != null &&
              Math.abs(job.llm_fit_score - (job.fit_score ?? 0)) >= 5 && (
                <span className="badge bg-signal/15 text-signal">
                  Scout: {Math.round(job.llm_fit_score)}
                </span>
              )}
            {job.source_urls?.length > 1 && (
              <span className="badge bg-white/[0.06] text-white/45">
                on {job.source_urls.length} boards
              </span>
            )}
            {/* Which of your profiles won this job — and so whose resume and
                cover letter it would be applied to with. Only shown when there
                is more than one profile in play; with one, it is the answer to
                a question nobody is asking. */}
            {showProfile && job.matched_profile_name && (
              <span
                className="badge bg-white/[0.06] text-white/55"
                title="Scored best against this profile — we'd apply with its resume"
              >
                {job.matched_profile_name}
              </span>
            )}
          </div>

          <p className="mt-1.5 font-mono text-[11px] text-white/40">
            {[
              job.company,
              job.location,
              job.remote ? "remote" : null,
              job.salary_text,
              job.source,
            ]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </button>

        <div className="flex flex-wrap items-center gap-1.5">
          <button
            className="btn-quiet"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            aria-label={expanded ? "Hide market and company context" : "Show market and company context"}
          >
            {expanded ? "Less" : "Intel"}
          </button>
          <button className="btn-quiet" onClick={onOpen}>
            Tailor
          </button>
          <ApplyActions job={job} onError={onError} />
          {job.url && (
            <a className="btn-quiet" href={job.url} target="_blank" rel="noreferrer noopener">
              Open
            </a>
          )}
          {job.status !== "APPLIED" && (
            <button className="btn-quiet" onClick={() => onTriage(job.id, "APPLIED")}>
              Applied
            </button>
          )}
          {job.status !== "DISMISSED" && (
            <button
              className="btn-quiet hover:text-bad"
              onClick={() => onTriage(job.id, "DISMISSED")}
              aria-label="Dismiss this job"
            >
              Dismiss
            </button>
          )}
        </div>
      </div>

      {expanded && <JobIntel jobId={job.id} />}
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/* Tailor drawer                                                              */
/* -------------------------------------------------------------------------- */

/**
 * Tailoring, against the job you are looking at, without leaving the feed.
 *
 * Tailoring used to be a top-level tab, which put an empty textarea between the
 * user and the thing they'd already chosen — you picked a job, then went to
 * another page to describe it. Here the posting is the input, so the drawer has
 * exactly one button. The full page still exists for pasting a description that
 * never came from the feed.
 */
function TailorDrawer({ job, onClose, onTriage, onError, toast }) {
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  useKeyboard({ Escape: onClose });

  async function run() {
    setBusy(true);
    try {
      const output = await api.tailor({
        job_posting_id: job.id,
        include_cover_letter: true,
      });
      setResult(output);
      // Tailoring is itself a triage signal: the user committed to this one.
      if (job.status === "NEW") await onTriage(job.id, "TAILORED");
      toast.success("Tailored — resume, cover letter and fit score are ready.");
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  /** Save a Blob under a filename, the only way a browser downloads bytes. */
  function save(blob, filename) {
    const href = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = href;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(href);
  }

  async function download(doc) {
    try {
      // The PDF is stored bytes, not text — it's what gets attached to an
      // application, and it's what an ATS parses.
      if (doc === "pdf") {
        save(await api.downloadTailoredPdf(result.tailored.id), result.tailored.pdf_filename || "tailored-resume.pdf");
        return;
      }
      if (doc === "cover_letter") {
        const letterId = result.cover_letter?.id;
        // Fall back to the letter stored on the tailoring row for runs made
        // before letters became their own artifact.
        const text = letterId
          ? await api.downloadCoverLetter(letterId)
          : await api.downloadTailored(result.tailored.id, "cover_letter");
        save(new Blob([text], { type: "text/markdown" }), "cover-letter.md");
        return;
      }
      const text = await api.downloadTailored(result.tailored.id, "resume");
      save(new Blob([text], { type: "text/markdown" }), "tailored-resume.md");
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    }
  }

  return (
    <div className="fixed inset-0 z-30 flex justify-end" role="dialog" aria-modal="true">
      <button
        className="absolute inset-0 bg-ink-900/70 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close"
        tabIndex={-1}
      />

      <aside className="relative flex h-full w-full max-w-xl flex-col overflow-y-auto border-l bg-ink-800 hairline">
        <header className="sticky top-0 z-10 space-y-2 border-b bg-ink-800/95 px-5 py-4 backdrop-blur hairline">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <p className="eyebrow">Tailor for</p>
              <h2 className="mt-1.5 font-display text-2xl leading-tight tracking-tight text-white">
                {job.title || "Untitled role"}
              </h2>
              <p className="mt-1 font-mono text-[11px] text-white/40">
                {[job.company, job.location, job.remote ? "remote" : null, job.salary_text]
                  .filter(Boolean)
                  .join(" · ") || "—"}
              </p>
            </div>
            <button className="btn-quiet shrink-0" onClick={onClose}>
              Close
            </button>
          </div>
        </header>

        <div className="flex-1 space-y-6 px-5 py-5">
          {!result ? (
            <div className="space-y-4">
              <p className="text-sm leading-relaxed text-white/50">
                We'll reorder your skills, re-angle your summary and draft a cover
                letter against this posting — using only what your resume already
                says. Nothing gets invented.
              </p>
              <div className="flex flex-wrap items-center gap-3">
                <button className="btn-primary" onClick={run} disabled={busy}>
                  {busy ? "Tailoring…" : "Tailor my resume"}
                </button>
                {job.url && (
                  <a
                    className="btn-quiet"
                    href={job.url}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    Read the posting
                  </a>
                )}
              </div>
              {busy && (
                <p className="text-xs text-signal">
                  Matching requirements against your resume, then writing…
                </p>
              )}
            </div>
          ) : (
            <TailorResult result={result} onDownload={download} onError={onError} />
          )}
        </div>

        <footer className="sticky bottom-0 border-t bg-ink-800/95 px-5 py-3 backdrop-blur hairline">
          <Link to={`/tailor?job=${job.id}`} className="btn-quiet">
            Open the full tailoring page
          </Link>
        </footer>
      </aside>
    </div>
  );
}

function TailorResult({ result, onDownload, onError }) {
  const { tailored, fit } = result;

  return (
    <div className="space-y-6">
      {fit && (
        <div className="space-y-4">
          <div className="flex items-start justify-between gap-4">
            <p className="eyebrow">Fit</p>
            <FitRing score={fit.overall} />
          </div>
          {fit.summary && (
            <p className="rounded-lg border bg-ink-900/50 px-4 py-3 text-sm leading-relaxed text-white/65 hairline">
              {fit.summary}
            </p>
          )}
          <FitBreakdown breakdown={fit.breakdown} />
        </div>
      )}

      <div className="space-y-3 border-t pt-5 hairline">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="eyebrow">Tailored summary</p>
          <CopyButton
            text={tailored.tailored_summary}
            label="Copy"
            onError={onError}
          />
        </div>
        <p className="text-sm leading-relaxed text-white/75">
          {tailored.tailored_summary}
        </p>
        <div className="flex flex-wrap gap-1.5">
          {tailored.ordered_skills.map((skill, index) => (
            <span
              key={skill}
              className={[
                "chip",
                index < 4 ? "border-signal/30 text-signal" : "text-white/50",
              ].join(" ")}
            >
              {skill}
            </span>
          ))}
        </div>
      </div>

      {tailored.missing_keywords?.length > 0 && (
        <div className="space-y-2 border-t pt-5 hairline">
          <p className="eyebrow">Asked for, not on your resume</p>
          <div className="flex flex-wrap gap-1.5">
            {tailored.missing_keywords.map((keyword) => (
              <span key={keyword} className="chip text-white/35">
                {keyword}
              </span>
            ))}
          </div>
        </div>
      )}

      <TailoredBullets experience={tailored.highlighted_experience} />

      {result.cover_letter && (
        <CoverLetterPanel letter={result.cover_letter} onError={onError} />
      )}

      <div className="flex flex-wrap gap-2 border-t pt-5 hairline">
        {tailored.has_pdf && (
          <button className="btn-primary" onClick={() => onDownload("pdf")}>
            Download PDF
          </button>
        )}
        <button
          className={tailored.has_pdf ? "btn-ghost" : "btn-primary"}
          onClick={() => onDownload("resume")}
        >
          Download Markdown
        </button>
        <button className="btn-ghost" onClick={() => onDownload("cover_letter")}>
          Download cover letter
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/**
 * The re-angled bullets, shown beside the originals.
 *
 * Both are rendered deliberately. The rewrite is an edit of the candidate's own
 * claims, and the only way for them to *check* that rather than take it on
 * trust is to see what the line used to say. A bullet the rewriter left alone
 * shows once — a diff of a thing against itself is just noise.
 */
function TailoredBullets({ experience }) {
  const roles = (experience || []).filter((role) => role.bullets?.length);
  if (!roles.length) return null;

  return (
    <div className="space-y-4 border-t pt-5 hairline">
      <p className="eyebrow">Bullets, re-angled for this role</p>
      {roles.map((role, index) => (
        <div key={`${role.company}-${index}`} className="space-y-2">
          <p className="font-mono text-[11px] text-white/40">
            {[role.title, role.company].filter(Boolean).join(" · ")}
          </p>
          <ul className="space-y-2">
            {role.bullets.map((bullet, i) => {
              const original = role.original_bullets?.[i];
              const changed = original && original !== bullet;
              return (
                <li key={i} className="text-sm leading-relaxed">
                  <span className="text-white/75">{bullet}</span>
                  {changed && (
                    <span className="mt-0.5 block text-[11px] leading-relaxed text-white/30 line-through decoration-white/20">
                      {original}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
      <p className="text-[10px] text-white/30">
        Struck-through text is what the bullet said before. Anything the rewrite
        tried to add that your resume doesn't support was rejected.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/**
 * The job-specific cover letter, with the company facts it was allowed to use.
 *
 * The research list is not decoration: the letter may only say things about the
 * employer that appear in it, so showing it is how a user checks a personalized
 * claim instead of trusting it — and spots one that's about the wrong company.
 */
function CoverLetterPanel({ letter, onError }) {
  const [body, setBody] = useState(letter.body || "");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [edited, setEdited] = useState(letter.edited);

  async function save() {
    setSaving(true);
    try {
      const updated = await api.editCoverLetter(letter.id, body);
      setBody(updated.body);
      setEdited(updated.edited);
      setEditing(false);
    } catch (err) {
      onError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3 border-t pt-5 hairline">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="eyebrow">Cover letter</p>
        <div className="flex items-center gap-2">
          {edited && <span className="badge bg-white/[0.06] text-white/45">edited by you</span>}
          {letter.generated_with === "heuristic" && (
            <span className="badge bg-white/[0.06] text-white/45">template</span>
          )}
          <CopyButton text={letter.full_text} label="Copy" onError={onError} />
          <button className="btn-quiet" onClick={() => setEditing((v) => !v)}>
            {editing ? "Cancel" : "Edit"}
          </button>
        </div>
      </div>

      {letter.greeting && (
        <p className="text-sm leading-relaxed text-white/60">{letter.greeting}</p>
      )}

      {editing ? (
        <div className="space-y-2">
          <textarea
            className="input min-h-[200px] w-full font-sans text-sm leading-relaxed"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            aria-label="Cover letter body"
          />
          <button className="btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save my wording"}
          </button>
          <p className="text-[10px] text-white/30">
            Once you edit it, regenerating won't overwrite your words.
          </p>
        </div>
      ) : (
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-white/75">{body}</p>
      )}

      {letter.sign_off && (
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-white/60">
          {letter.sign_off}
        </p>
      )}

      {letter.company_research?.length > 0 && (
        <details className="rounded-lg border border-white/[0.08] bg-white/[0.02] px-3 py-2">
          <summary className="cursor-pointer text-[11px] text-white/45">
            What the personalization is based on ({letter.company_research.length})
          </summary>
          <ul className="mt-2 space-y-1">
            {letter.company_research.map((fact, i) => (
              <li key={i} className="text-[11px] leading-relaxed text-white/55">
                {fact}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Filters({ filters, onChange }) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="min-w-[180px] flex-1">
        <label className="label" htmlFor="filter-company">
          Company
        </label>
        <input
          id="filter-company"
          className="input"
          value={filters.company}
          onChange={(e) => onChange({ ...filters, company: e.target.value })}
          placeholder="Any"
        />
      </div>
      <div className="w-32">
        <label className="label" htmlFor="filter-fit">
          Min fit
        </label>
        <input
          id="filter-fit"
          type="number"
          min="0"
          max="100"
          className="input"
          value={filters.min_fit}
          onChange={(e) => onChange({ ...filters, min_fit: e.target.value })}
          placeholder="0"
        />
      </div>
      <div className="w-40">
        <label className="label" htmlFor="filter-status">
          Status
        </label>
        <select
          id="filter-status"
          className="input"
          value={filters.status}
          onChange={(e) => onChange({ ...filters, status: e.target.value })}
        >
          <option value="" className="bg-ink-800">
            Active
          </option>
          {Object.keys(STATUS_STYLE).map((key) => (
            <option key={key} value={key} className="bg-ink-800">
              {STATUS_STYLE[key][0]}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

function SearchManager({ searches, scanning, providers, onRun, onChange, onError, toast }) {
  const confirm = useConfirm();
  const [open, setOpen] = useState(false);
  const [roles, setRoles] = useState("");
  const [keywords, setKeywords] = useState("");
  const [location, setLocation] = useState("");
  const [remoteOnly, setRemoteOnly] = useState(false);
  const [minFit, setMinFit] = useState(60);
  const [busy, setBusy] = useState(false);

  const split = (value) =>
    value
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean);

  async function create() {
    onError(null);
    setBusy(true);
    try {
      await api.createSearch({
        roles: split(roles),
        keywords: split(keywords),
        location: location.trim() || null,
        remote_only: remoteOnly,
        min_fit_score: Number(minFit),
      });
      setRoles("");
      setKeywords("");
      setLocation("");
      setOpen(false);
      await onChange();
      toast.success("Search created — scanning now.");
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(search) {
    if (
      !(await confirm({
        title: "Delete this search?",
        message: `"${search.name}" will stop scanning. Jobs already found stay in your feed.`,
        confirmLabel: "Delete search",
        tone: "danger",
      }))
    )
      return;
    try {
      await api.deleteSearch(search.id);
      await onChange();
      toast.success("Search deleted.");
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    }
  }

  async function toggle(search) {
    try {
      await api.patchSearch(search.id, { is_active: !search.is_active });
      await onChange();
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    }
  }

  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="eyebrow">Saved searches</p>
        <button className="btn-quiet" onClick={() => setOpen((v) => !v)}>
          {open ? "Cancel" : "New search"}
        </button>
      </div>

      {searches.length > 0 && (
        <ul className="space-y-2">
          {searches.map((search) => (
            <li
              key={search.id}
              className="panel flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-white/85">{search.name}</p>
                <p className="font-mono text-[11px] text-white/35">
                  {[
                    `${search.min_fit_score}+ fit`,
                    `every ${search.interval_hours}h`,
                    search.location,
                    search.remote_only ? "remote only" : null,
                    `${search.jobs_found} found`,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </div>

              <span
                className={[
                  "badge",
                  search.is_active
                    ? "bg-good/15 text-good"
                    : "bg-white/[0.06] text-white/35",
                ].join(" ")}
              >
                {search.is_active ? "active" : "paused"}
              </span>

              <button
                className="btn-quiet"
                onClick={() => onRun(search.id)}
                disabled={scanning === search.id}
              >
                {scanning === search.id ? "Scanning…" : "Scan now"}
              </button>
              <button className="btn-quiet" onClick={() => toggle(search)}>
                {search.is_active ? "Pause" : "Resume"}
              </button>
              <button
                className="btn-quiet hover:text-bad"
                onClick={() => remove(search)}
                aria-label={`Delete ${search.name}`}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      {open && (
        <div className="panel space-y-4 p-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="search-roles">
                Roles
              </label>
              <input
                id="search-roles"
                className="input"
                value={roles}
                onChange={(e) => setRoles(e.target.value)}
                placeholder="Senior Backend Engineer, Staff Engineer"
              />
            </div>
            <div>
              <label className="label" htmlFor="search-keywords">
                Keywords
              </label>
              <input
                id="search-keywords"
                className="input"
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
                placeholder="python, fastapi, fintech"
              />
            </div>
            <div>
              <label className="label" htmlFor="search-location">
                Location
              </label>
              <input
                id="search-location"
                className="input"
                value={location}
                onChange={(e) => setLocation(e.target.value)}
                placeholder="San Francisco, or leave blank"
              />
            </div>
            <div>
              <label className="label" htmlFor="search-fit">
                Minimum fit score
              </label>
              <input
                id="search-fit"
                type="number"
                min="0"
                max="100"
                className="input"
                value={minFit}
                onChange={(e) => setMinFit(e.target.value)}
              />
            </div>
          </div>

          <label className="flex cursor-pointer items-start gap-3 text-sm">
            <input
              type="checkbox"
              checked={remoteOnly}
              onChange={(e) => setRemoteOnly(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 accent-signal"
            />
            <span className="text-white/55">Remote roles only</span>
          </label>

          <div className="flex items-center gap-3 border-t pt-4 hairline">
            <button
              className="btn-primary"
              onClick={create}
              disabled={busy || (!roles.trim() && !keywords.trim())}
            >
              {busy ? "Scanning…" : "Create and scan"}
            </button>
            <span className="text-xs text-white/30">
              We'll re-scan on a schedule and score every result.
            </span>
          </div>
        </div>
      )}

      {providers && !providers.google_jobs_serpapi && (
        <p className="text-xs leading-relaxed text-white/25">
          Searching {providers.public_boards.join(", ")}. Set SERPAPI_API_KEY on the
          server to add Google Jobs (Indeed, LinkedIn, Workday).
        </p>
      )}
    </section>
  );
}

function EmptyFeed({ hasSearch }) {
  return (
    <div className="panel px-6 py-16 text-center">
      <p className="font-display text-2xl tracking-tight text-white/80">
        {hasSearch ? "Nothing above your threshold yet" : "No searches yet"}
      </p>
      <p className="mx-auto mt-2 max-w-sm text-sm text-white/40">
        {hasSearch
          ? "Scan again later, or lower the minimum fit score to widen the net."
          : "Create a saved search and we'll scan the boards for roles that actually match your resume."}
      </p>
    </div>
  );
}
