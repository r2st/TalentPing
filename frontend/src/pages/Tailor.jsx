import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { FitBreakdown, FitRing, KeywordSplit } from "../components/FitScore";
import CopyButton from "../components/ui/CopyButton";
import ErrorBanner from "../components/ui/ErrorBanner";
import { useToast } from "../components/ui/Toast";
import { api } from "../lib/api";

/**
 * Smart Apply — paste a job, get a resume tailored to it.
 *
 * The page is deliberately one column and one action: input on the left, result
 * below. Everything the AI produced is editable-by-download rather than locked
 * in the UI, because the candidate is the one who has to defend it in an
 * interview.
 */
export default function Tailor() {
  const toast = useToast();
  const [params] = useSearchParams();
  const [resumes, setResumes] = useState([]);
  const [history, setHistory] = useState([]);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const [mode, setMode] = useState("text"); // text | url
  const [description, setDescription] = useState("");
  const [url, setUrl] = useState("");
  const [resumeId, setResumeId] = useState(null);
  // Set when arriving from the job feed's "Tailor" button.
  const jobPostingId = params.get("job") ? Number(params.get("job")) : null;

  const refresh = useCallback(async () => {
    const [resumeList, tailored] = await Promise.all([
      api.listResumes(),
      api.listTailored(),
    ]);
    setResumes(resumeList);
    setHistory(tailored);
  }, []);

  useEffect(() => {
    refresh().catch((err) => setError(err.message));
  }, [refresh]);

  // Arriving from the feed: pull the posting in so the user sees what they picked.
  useEffect(() => {
    if (!jobPostingId) return;
    api
      .getJob(jobPostingId)
      .then((job) => {
        setDescription(job.description || "");
        if (job.url) setUrl(job.url);
      })
      .catch(() => {});
  }, [jobPostingId]);

  const canRun = Boolean(jobPostingId || description.trim() || url.trim());

  async function run() {
    setError(null);
    setBusy(true);
    setResult(null);
    try {
      const payload = { resume_id: resumeId ?? undefined };
      if (jobPostingId) payload.job_posting_id = jobPostingId;
      if (description.trim()) payload.job_description = description.trim();
      else if (url.trim()) payload.job_url = url.trim();

      setResult(await api.tailor(payload));
      await refresh();
      toast.success("Tailored — resume, cover letter and fit score are ready.");
    } catch (err) {
      setError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function download(id, doc) {
    try {
      const text = await api.downloadTailored(id, doc);
      const blob = new Blob([text], {
        type: doc === "resume" ? "text/markdown" : "text/plain",
      });
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = doc === "resume" ? "tailored-resume.md" : "cover-letter.txt";
      link.click();
      URL.revokeObjectURL(href);
    } catch (err) {
      setError(err.message);
      toast.error(err.message);
    }
  }

  return (
    <div className="space-y-10">
      <header className="animate-fade-up">
        <p className="eyebrow">Smart Apply</p>
        <h1 className="mt-3 font-display text-4xl leading-none tracking-tightest text-white">
          Tailor your resume to one job
        </h1>
        <p className="mt-3 max-w-xl text-sm leading-relaxed text-white/45">
          Paste a job description and we reorder your skills, re-angle your
          summary, and draft a cover letter — using only what your resume already
          says. Nothing gets invented.
        </p>
      </header>

      <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>

      {resumes.length === 0 ? (
        <NoResume />
      ) : (
        <section className="panel space-y-5 p-5">
          {jobPostingId && (
            <p className="rounded-lg border border-signal/25 bg-signal/[0.07] px-3 py-2 text-xs text-signal">
              Tailoring against the job you picked from the feed.
            </p>
          )}

          <div className="flex gap-1">
            {[
              ["text", "Paste the description"],
              ["url", "Use a job URL"],
            ].map(([key, label]) => (
              <button
                key={key}
                onClick={() => setMode(key)}
                className={[
                  "rounded-md px-3 py-1.5 text-xs transition-colors",
                  mode === key
                    ? "bg-white/[0.07] text-white"
                    : "text-white/35 hover:text-white/70",
                ].join(" ")}
              >
                {label}
              </button>
            ))}
          </div>

          {mode === "text" ? (
            <div>
              <label className="label" htmlFor="jd">
                Job description
              </label>
              <textarea
                id="jd"
                className="input min-h-[180px] resize-y font-mono text-xs leading-relaxed"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Paste the full posting — requirements, responsibilities, everything."
              />
            </div>
          ) : (
            <div>
              <label className="label" htmlFor="jd-url">
                Job URL
              </label>
              <input
                id="jd-url"
                className="input"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://company.com/careers/senior-backend-engineer"
              />
              <p className="mt-2 text-xs text-white/30">
                Postings behind a login or heavy JavaScript won't fetch — paste
                the text instead if it fails.
              </p>
            </div>
          )}

          {resumes.length > 1 && (
            <div>
              <label className="label" htmlFor="tailor-resume">
                Tailor which resume
              </label>
              <select
                id="tailor-resume"
                className="input"
                value={resumeId ?? resumes.find((r) => r.is_default)?.id ?? ""}
                onChange={(e) => setResumeId(Number(e.target.value))}
              >
                {resumes.map((resume) => (
                  <option key={resume.id} value={resume.id} className="bg-ink-800">
                    {resume.headline || resume.filename || `Resume #${resume.id}`}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="flex items-center gap-3 border-t pt-4 hairline">
            <button className="btn-primary" onClick={run} disabled={!canRun || busy}>
              {busy ? "Tailoring…" : "Tailor my resume"}
            </button>
            <span className="text-xs text-white/30">
              {canRun
                ? "You'll get a tailored resume, a cover letter, and a fit score."
                : "Add a job description or URL to begin."}
            </span>
          </div>
        </section>
      )}

      {busy && <Working />}
      {result && (
        <Result
          result={result}
          onDownload={download}
          onCopyResume={() => api.downloadTailored(result.tailored.id, "resume")}
          onError={(msg) => {
            setError(msg);
            toast.error(msg);
          }}
        />
      )}

      {history.length > 0 && (
        <History rows={history} onDownload={download} />
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Result({ result, onDownload, onCopyResume, onError }) {
  const { tailored, parsed_job: job, fit } = result;

  return (
    <div className="stagger space-y-4">
      <section className="panel space-y-5 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="eyebrow">Tailored for</p>
            <h2 className="mt-2 font-display text-2xl tracking-tight text-white">
              {job.title || "this role"}
            </h2>
            <p className="mt-1 font-mono text-xs text-white/40">
              {[job.company, job.location, job.remote ? "remote" : null, job.salary_text]
                .filter(Boolean)
                .join(" · ") || "—"}
            </p>
          </div>
          {fit && <FitRing score={fit.overall} />}
        </div>

        {fit?.summary && (
          <p className="rounded-lg border bg-ink-900/50 px-4 py-3 text-sm leading-relaxed text-white/65 hairline">
            {fit.summary}
          </p>
        )}
      </section>

      {fit && (
        <section className="panel space-y-5 p-5">
          <p className="eyebrow">Why this score</p>
          <FitBreakdown breakdown={fit.breakdown} />
        </section>
      )}

      <section className="panel space-y-5 p-5">
        <p className="eyebrow">Keyword coverage</p>
        <KeywordSplit
          matched={tailored.matched_keywords}
          missing={tailored.missing_keywords}
        />
      </section>

      <section className="panel space-y-4 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="eyebrow">Tailored summary</p>
          <div className="flex items-center gap-2">
            <span className="badge bg-white/[0.06] text-white/40">
              {tailored.generated_with === "llm" ? "ai-written" : "rule-based"}
            </span>
            <CopyButton
              text={tailored.tailored_summary}
              label="Copy summary"
              onError={onError}
            />
          </div>
        </div>
        <p className="text-sm leading-relaxed text-white/75">
          {tailored.tailored_summary}
        </p>

        <div>
          <p className="eyebrow mb-2">Skills, reordered for this role</p>
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

        {tailored.highlighted_experience?.length > 0 && (
          <div>
            <p className="eyebrow mb-2">Experience to lead with</p>
            <ul className="space-y-2">
              {tailored.highlighted_experience.map((entry, index) => (
                <li
                  key={`${entry.company}-${index}`}
                  className="rounded-lg border bg-ink-900/40 px-4 py-3 hairline"
                >
                  <p className="text-sm text-white/85">
                    {[entry.title, entry.company].filter(Boolean).join(" — ")}
                  </p>
                  {entry.why_relevant && (
                    <p className="mt-1 text-xs leading-relaxed text-white/40">
                      {entry.why_relevant}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section className="panel space-y-4 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="eyebrow">Cover letter</p>
          <CopyButton
            text={tailored.cover_letter}
            label="Copy cover letter"
            onError={onError}
          />
        </div>
        <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed text-white/75">
          {tailored.cover_letter}
        </pre>
        <div className="flex flex-wrap gap-2 border-t pt-4 hairline">
          <button className="btn-primary" onClick={() => onDownload(tailored.id, "resume")}>
            Download resume
          </button>
          <CopyButton
            text={onCopyResume}
            label="Copy resume"
            className="btn-ghost"
            onError={onError}
          />
          <button
            className="btn-ghost"
            onClick={() => onDownload(tailored.id, "cover_letter")}
          >
            Download cover letter
          </button>
        </div>
      </section>
    </div>
  );
}

function History({ rows, onDownload }) {
  return (
    <section className="space-y-2">
      <p className="eyebrow">Earlier runs</p>
      <ul className="space-y-2">
        {rows.map((row) => (
          <li
            key={row.id}
            className="panel flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm text-white/85">
                {row.job_title || "Untitled role"}
              </p>
              <p className="font-mono text-[11px] text-white/35">
                {[row.job_company, new Date(row.created_at).toLocaleDateString()]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            </div>
            <button className="btn-quiet" onClick={() => onDownload(row.id, "resume")}>
              Resume
            </button>
            <button
              className="btn-quiet"
              onClick={() => onDownload(row.id, "cover_letter")}
            >
              Cover letter
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function NoResume() {
  return (
    <div className="panel px-6 py-14 text-center">
      <p className="font-display text-2xl tracking-tight text-white/80">
        Upload a resume first
      </p>
      <p className="mx-auto mt-2 max-w-sm text-sm text-white/40">
        Tailoring works from what your resume already says — so it needs one to
        work from.
      </p>
      {/* A plain <a> here reloaded the whole app — bundle, auth check and all —
          to reach a route the router already owns. */}
      <Link to="/setup" className="btn-ghost mt-6">
        Go to setup
      </Link>
    </div>
  );
}

function Working() {
  return (
    <div className="panel relative overflow-hidden px-6 py-10 text-center">
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-white/[0.04] to-transparent" />
      <p className="text-sm text-signal">Reading the posting and tailoring…</p>
      <p className="mt-2 text-xs text-white/30">
        Matching requirements against your resume, then writing.
      </p>
    </div>
  );
}
