import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import PreferencesForm, {
  preferencePayload,
  SparkleGlyph,
} from "../components/PreferencesForm";
import ProfileManager from "../components/ProfileManager";
import Step from "../components/Step";
import { useConfirm } from "../components/ui/ConfirmDialog";
import ErrorBanner from "../components/ui/ErrorBanner";
import FilePreview from "../components/ui/FilePreview";
import useFilePreview from "../components/ui/useFilePreview";
import SkeletonLoader from "../components/ui/SkeletonLoader";
import { useToast } from "../components/ui/Toast";
import { api } from "../lib/api";
import { formatWhen } from "../lib/format";

/**
 * The whole product setup, in three steps: drop in your resumes → confirm the
 * search we read off them → connect Gmail and go.
 *
 * Resumes come first because they are the step that does work for the user.
 * Everything the search needs — the titles to chase, the skills, the seniority,
 * where you are, what you expect to be paid — is extracted from the files, so
 * step two is a review rather than thirteen empty fields. Gmail is asked for
 * last, when there is finally something to send.
 */

// What a resume may arrive as. Kept in step with `_ACCEPTED_SUFFIXES` on the
// server: a candidate's resume lives in Word until the moment it is sent, and
// rejecting the .docx sitting in their Documents folder sends them off to export
// a PDF before the product will talk to them.
const ACCEPTED = /\.(pdf|docx)$/i;
// For the file picker. The MIME types are listed alongside the extensions
// because macOS Safari filters on type and Windows Chrome on extension.
const ACCEPT_ATTR = [
  "application/pdf",
  ".pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".docx",
].join(",");

// The server's step names, in the order they are reached. Steps 3 and 4 are one
// row in the UI: connecting a mailbox and switching on are a single decision.
const SEQUENCE = [
  "upload_resume",
  "set_preferences",
  "connect_email",
  "start_autopilot",
];

/** Whether a UI row covering `names` is done, active, or not yet reachable. */
export function stepState(nextStep, names) {
  if (nextStep === "done") return "done";
  const current = SEQUENCE.indexOf(nextStep);
  const first = SEQUENCE.indexOf(names[0]);
  const last = SEQUENCE.indexOf(names[names.length - 1]);
  if (current < 0) return "todo";
  if (current > last) return "done";
  if (current >= first) return "active";
  return "todo";
}

export default function Setup() {
  const navigate = useNavigate();
  const [status, setStatus] = useState(null);
  const [resumes, setResumes] = useState([]);
  const [profiles, setProfiles] = useState([]);
  const [error, setError] = useState(null);
  // Distinct from `error`: this one means we never got a page at all, and the
  // only useful thing to render is why, plus a way to try again.
  const [loadError, setLoadError] = useState(null);

  const refresh = useCallback(async () => {
    const [onboarding, resumeList, profileList] = await Promise.all([
      api.onboarding(),
      api.listResumes(),
      // Never fatal to the page: profiles are one section of it, and a user
      // whose profile list fails to load should still be able to upload a
      // resume and connect their mailbox.
      api.listProfiles().catch(() => []),
    ]);
    setStatus(onboarding);
    setResumes(resumeList);
    setProfiles(profileList);
    setLoadError(null);
    return onboarding;
  }, []);

  useEffect(() => {
    refresh().catch((err) => setLoadError(err.message));
  }, [refresh]);

  // A failed first read used to leave this page on its skeleton forever — the
  // error banner lived below the early return, so the one screen holding every
  // resume and preference simply rendered as nothing. Say what happened.
  if (loadError && !status)
    return (
      <div className="space-y-5">
        <header>
          <p className="eyebrow">Setup</p>
          <h1 className="mt-3 font-display text-4xl leading-none tracking-tightest text-white">
            We couldn't load your setup.
          </h1>
        </header>
        <ErrorBanner>{loadError}</ErrorBanner>
        <button
          className="btn-primary"
          onClick={() => refresh().catch((err) => setLoadError(err.message))}
        >
          Try again
        </button>
      </div>
    );

  if (!status) return <SkeletonLoader rows={[96, 96, 96]} shimmer />;

  const step = status.next_step;

  return (
    <div className="space-y-8">
      <header className="animate-fade-up">
        <p className="eyebrow">Setup</p>
        <h1 className="mt-3 font-display text-4xl leading-none tracking-tightest text-white">
          {status.complete ? "You're all set." : "Three steps to your first ping."}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-white/45">
          {status.complete
            ? "Autopilot is running. Adjust anything below, or watch the replies come in."
            : "Upload your resumes and we read the rest off them — the roles, the skills, the seniority, where you are. Check what we got, connect your mailbox, and walk away."}
        </p>
      </header>

      <ErrorBanner onDismiss={() => setError(null)}>{error}</ErrorBanner>

      <div className="stagger space-y-3">
        <UploadResumeStep
          state={stepState(step, ["upload_resume"])}
          resumes={resumes}
          onChange={refresh}
          onError={setError}
        />
        <PreferencesStep
          state={stepState(step, ["set_preferences"])}
          resumes={resumes}
          profiles={profiles}
          onSaved={refresh}
          onError={setError}
        />
        <LaunchStep
          state={stepState(step, ["connect_email", "start_autopilot"])}
          status={status}
          onChange={refresh}
          onStarted={async () => {
            await refresh();
            // Autopilot no longer has a page of its own — its switch and its
            // counters live on the pipeline, which is also what there is to
            // look at once it starts running.
            navigate("/pipeline");
          }}
          onError={setError}
        />
      </div>

      <LinkedInPanel onError={setError} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 1 — resumes                                                            */
/* -------------------------------------------------------------------------- */

function UploadResumeStep({ state, resumes, onChange, onError }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  // The resume currently being promoted or deleted, so its row's buttons can't
  // be fired twice while the request is in flight.
  const [acting, setActing] = useState(null);
  const inputRef = useRef(null);

  async function upload(fileList) {
    const dropped = Array.from(fileList || []);
    const files = dropped.filter((f) => ACCEPTED.test(f.name));
    if (!files.length) {
      // Name the legacy Word format specifically: "resumes need to be PDF or
      // .docx" is no help at all to someone holding a .doc, and Word's own
      // Save As is the whole fix.
      onError(
        dropped.some((f) => /\.docx?$/i.test(f.name) && !ACCEPTED.test(f.name))
          ? 'Word 97-2003 (.doc) files can\'t be read — use "Save As" in Word to make a .docx or a PDF.'
          : "Resumes need to be a PDF or a Word .docx.",
      );
      return;
    }
    onError(null);
    setBusy(true);
    try {
      await api.uploadResumes(files);
      await onChange();
      toast.success(
        `Read ${files.length} resume${files.length === 1 ? "" : "s"}.`,
      );
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(id, name) {
    if (
      !(await confirm({
        title: "Delete this resume?",
        message: `${name} and anything tailored from it will be removed. This can't be undone.`,
        confirmLabel: "Delete resume",
        tone: "danger",
      }))
    )
      return;
    onError(null);
    setActing(id);
    try {
      await api.deleteResume(id);
      await onChange();
      toast.success(`${name} deleted.`);
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setActing(null);
    }
  }

  // Promoting used to be silent: the request went out, the list re-rendered, and
  // nothing said it had worked. On two resumes parsed to the same headline that
  // was indistinguishable from a dead button — which is what it was reported as.
  async function makeDefault(id, name) {
    onError(null);
    setActing(id);
    try {
      await api.patchResume(id, { is_default: true });
      await onChange();
      toast.success(`${name} is now your default resume.`);
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setActing(null);
    }
  }

  const summary =
    resumes.length > 0
      ? `${resumes.length} resume${resumes.length === 1 ? "" : "s"}`
      : null;

  return (
    <Step index={1} title="Add your resumes" state={state} summary={summary}>
      <div className="space-y-4">
        {resumes.length > 0 && (
          <ul className="space-y-2">
            {resumes.map((resume) => (
              <ResumeCard
                key={resume.id}
                resume={resume}
                busy={acting === resume.id}
                onRemove={remove}
                onMakeDefault={makeDefault}
              />
            ))}
          </ul>
        )}

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            upload(e.dataTransfer.files);
          }}
          className={[
            "rounded-xl border border-dashed px-5 py-8 text-center transition-colors",
            dragging
              ? "border-signal/60 bg-signal/[0.06]"
              : "border-white/10 hover:border-white/20",
          ].join(" ")}
        >
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPT_ATTR}
            multiple
            className="sr-only"
            aria-label="Resume files"
            onChange={(e) => upload(e.target.files)}
          />
          <p className="text-sm text-white/55">
            {busy ? (
              <span className="text-signal">Reading your resumes…</span>
            ) : (
              <>
                Drop your PDFs or Word files here, or{" "}
                <button
                  className="text-signal underline-offset-4 hover:underline"
                  onClick={() => inputRef.current?.click()}
                >
                  browse
                </button>
              </>
            )}
          </p>
          <p className="mt-2 text-xs text-white/30">
            Several at once is fine — one per role you're going after. We pull out
            the titles, skills, seniority, location and any salary you've stated,
            and build the search from all of them together.
          </p>
        </div>
      </div>
    </Step>
  );
}

/**
 * What the server will hand back for this resume, under the name it arrives as.
 *
 * The upload's own name when the upload is on file. Otherwise the document is a
 * PDF rendered from the parse whatever the original was called, so a row whose
 * filename says `.docx` would otherwise offer to save a PDF as a Word file.
 */
export function previewFilename(resume) {
  if (resume.has_original_file && resume.filename) return resume.filename;
  const stem = (resume.filename || "resume").replace(/\.[^.]+$/, "");
  return `${stem}.pdf`;
}

/**
 * Open the resume itself.
 *
 * Setup could take a file and then only ever describe it back — a headline, a
 * filename, a row of skills. Which is no way to tell two uploads of one CV
 * apart, and no way at all to check that the document about to go out under the
 * candidate's name is the one they meant to send. So it opens, in the same
 * overlay the inbox opens an attachment in, off the same kind of authenticated
 * fetch.
 *
 * A Word resume is fetched twice over: the upload itself, which is what the
 * download hands over and what a recruiter receives, and the server's rendering
 * of it, which is the only way to actually read a .docx in a page. A PDF costs
 * one request, exactly as it did before conversion existed.
 *
 * The blobs outlive the fetch: they are released when the preview closes or the
 * row unmounts, not when this handler returns.
 */
function PreviewResumeButton({ resume, name, disabled }) {
  const toast = useToast();
  const filename = previewFilename(resume);
  const preview = useFilePreview({
    name: filename,
    fetchFile: () => api.resumeFile(resume.id),
    fetchPreview: () => api.resumePreview(resume.id),
    // A toast rather than the step's error banner: failing to open a document
    // says nothing about the upload, which is still on file and still the one
    // that will be sent.
    onError: (err) => toast.error(err.message),
  });

  return (
    <>
      <button
        type="button"
        className="btn-quiet"
        disabled={disabled || preview.busy}
        aria-label={`Preview ${name}`}
        onClick={preview.open}
      >
        {preview.busy ? "Opening…" : "Preview"}
      </button>
      {preview.url && (
        <FilePreview
          name={filename}
          url={preview.url}
          previewUrl={preview.previewUrl}
          label="Resume"
          onClose={preview.close}
        />
      )}
    </>
  );
}

/**
 * One uploaded resume, with the three things you can do to it.
 *
 * Reading it comes first, because the other two are decisions about a document
 * the page would otherwise only describe: which of two same-headline uploads to
 * make default, and which to delete, are both unanswerable without opening them.
 *
 * Make-default and delete used to be all but unusable. "Make default" was a 10px
 * run of uppercase mono at 30% opacity — it read as a caption rather than a
 * control, and its hit box was small enough that a click aimed at the words
 * missed it entirely. Delete was a bare `×` glyph, measured at 9x23px: a third of
 * the 24x24 minimum, with no word on it saying what it did. They are ordinary
 * buttons now, sized and labelled like every other button in the product.
 *
 * The filename is shown next to the headline because it is the only thing that
 * tells two resumes apart. Both are parsed to the same headline whenever a
 * candidate uploads variants of one CV — which is exactly when picking the right
 * default matters, and the list was showing two identical rows.
 */
function ResumeCard({ resume, busy, onRemove, onMakeDefault }) {
  const name = resume.headline || resume.filename || `Resume #${resume.id}`;
  // Only worth showing when it adds something the headline didn't.
  const file = resume.filename && resume.filename !== name ? resume.filename : null;

  return (
    <li className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3 rounded-lg border bg-ink-700/50 px-4 py-3 hairline">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm font-medium text-white">{name}</span>
          {resume.is_default && (
            <span className="badge bg-signal/15 text-signal">default</span>
          )}
        </div>

        <p className="mt-1 font-mono text-xs text-white/35">
          {[
            file,
            resume.full_name,
            resume.years_experience ? `${resume.years_experience} yrs` : null,
            resume.seniority,
            resume.location,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>

        {resume.skills?.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {resume.skills.slice(0, 8).map((skill) => (
              <span key={skill} className="chip">
                {skill}
              </span>
            ))}
            {resume.skills.length > 8 && (
              <span className="chip text-white/35">+{resume.skills.length - 8}</span>
            )}
          </div>
        )}
      </div>

      {/* Both carry an aria-label naming the resume: the visible words have to
          be the same on every row, so without it a screen reader (and a test)
          hears "Delete" four times with no way to tell which is which. */}
      <div className="flex shrink-0 items-center gap-1">
        <PreviewResumeButton resume={resume} name={name} disabled={busy} />
        {!resume.is_default && (
          <button
            type="button"
            className="btn-quiet"
            disabled={busy}
            aria-label={`Make ${name} the default`}
            onClick={() => onMakeDefault(resume.id, name)}
          >
            Make default
          </button>
        )}
        <button
          type="button"
          className="btn-quiet hover:bg-bad/10 hover:text-bad"
          disabled={busy}
          aria-label={`Delete ${name}`}
          onClick={() => onRemove(resume.id, name)}
        >
          Delete
        </button>
      </div>
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 2 — confirm the search                                                 */
/* -------------------------------------------------------------------------- */

/**
 * The one configuration screen in the product, rendered by the shared
 * PreferencesForm — but never as a blank form. On a first visit we read the
 * preferences implied by *every* uploaded resume and pre-fill from them, so the
 * step is a review: suggested values are tinted and badged, and the first edit
 * to a field drops its badge.
 *
 * Uploading another resume re-merges and fills whatever the user hasn't touched.
 * A field they have edited is never written over — their answer outranks ours.
 */
function PreferencesStep({ state, resumes, profiles, onSaved, onError }) {
  const toast = useToast();
  const [prefs, setPrefs] = useState(null);
  const [busy, setBusy] = useState(false);
  // Field names currently showing a "from resume" badge, plus the extraction the
  // suggestions came from.
  const [suggested, setSuggested] = useState([]);
  const [notes, setNotes] = useState({});
  const [profile, setProfile] = useState(null);
  // The resume set the current suggestions were built from, so adding or
  // removing one re-suggests and nothing else does.
  const [suggestedFor, setSuggestedFor] = useState(null);
  // Fields the user has touched — never re-filled, never badged again.
  const touched = useRef(new Set());

  const resumeKey = useMemo(
    () => resumes.map((r) => r.id).sort((a, b) => a - b).join(","),
    [resumes],
  );

  // Only fetch once the step is reachable; GET creates the row lazily and we
  // don't want that happening behind a step the user hasn't got to yet.
  useEffect(() => {
    if (state === "todo" || prefs) return;
    api
      .getAutopilot()
      .then(setPrefs)
      .catch((err) => onError(err.message));
  }, [state, prefs, onError]);

  // Pre-fill from the resumes, but only before the user has ever saved: after
  // that the stored row is their answer and we must not talk over it.
  useEffect(() => {
    if (!prefs || prefs.configured_at || !resumeKey) return;
    if (suggestedFor === resumeKey) return;
    let cancelled = false;
    api
      .mergedSuggestedPreferences()
      .then((suggestion) => {
        if (cancelled) return;
        setSuggestedFor(resumeKey);
        setNotes(suggestion.notes || {});
        setProfile(suggestion.profile || null);
        setPrefs((prev) => {
          const { values, filled } = applySuggestions(prev, suggestion, {
            touched: touched.current,
            replaceable: suggested,
          });
          setSuggested(filled);
          return values;
        });
      })
      .catch(() => !cancelled && setSuggestedFor(resumeKey));
    return () => {
      cancelled = true;
    };
  }, [prefs, resumeKey, suggestedFor, suggested]);

  const set = (patch) => {
    for (const name of Object.keys(patch)) touched.current.add(name);
    setSuggested((prev) => prev.filter((name) => !(name in patch)));
    setPrefs((prev) => ({ ...prev, ...patch }));
  };

  async function save() {
    onError(null);
    setBusy(true);
    try {
      await api.updateAutopilot({
        ...preferencePayload(prefs),
        resume_id: prefs.resume_id ?? resumes.find((r) => r.is_default)?.id ?? null,
      });
      toast.success("Search saved.");
      await onSaved();
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Step index={2} title="Confirm your search" state={state}>
      {state === "todo" || !prefs ? null : (
        <div className="space-y-5">
          <ExtractedProfile profile={profile} />
          <PrefillBanner fields={suggested} />

          <MultiProfileNotice count={profiles.length} />

          <PreferencesForm
            prefs={prefs}
            resumes={resumes}
            onChange={set}
            suggested={suggested}
            notes={notes}
            sliderDebounce={150}
          />

          <div className="flex items-center gap-3 border-t pt-4 hairline">
            <button className="btn-primary" onClick={save} disabled={busy}>
              {busy
                ? "Saving…"
                : suggested.length > 0
                  ? "Looks right — save"
                  : "Save search"}
            </button>
            <span className="text-xs text-white/30">You can change any of this later.</span>
          </div>

          <section className="space-y-3 border-t pt-5 hairline">
            <div>
              <p className="eyebrow">Profiles</p>
              <h3 className="mt-1.5 text-base text-white">
                Going after more than one kind of role?
              </h3>
            </div>
            <ProfileManager
              profiles={profiles}
              resumes={resumes}
              onChange={onSaved}
              onError={onError}
            />
          </section>
        </div>
      )}
    </Step>
  );
}

/**
 * Says which screen is in charge once there is more than one profile.
 *
 * With a single profile the fields above and the profile below are the same
 * intent, kept in step by the server. With several, "my roles" stops having one
 * answer — so the roles, places and salary above become a fallback nobody reads,
 * and saying so is better than letting someone edit a field that no longer does
 * anything.
 */
function MultiProfileNotice({ count }) {
  if (count < 2) return null;
  return (
    <div className="rounded-lg border border-white/10 bg-ink-900/40 px-3.5 py-3">
      <p className="text-xs leading-relaxed text-white/45">
        You have {count} profiles, so the roles, locations and salary below are no
        longer what we search on — each profile carries its own. The limits,
        sending and follow-up settings still apply to everything.
      </p>
    </div>
  );
}

/* ---- Resume-driven pre-fill helpers -------------------------------------- */

/** Human names for the fields, for the "we filled these in" line. */
const FIELD_LABELS = {
  target_roles: "roles",
  locations: "where",
  target_industries: "industries",
  remote_only: "remote only",
  salary_min: "minimum salary",
};

function isBlank(value) {
  if (Array.isArray(value)) return value.length === 0;
  return value === null || value === undefined || value === "" || value === false;
}

/**
 * Merge suggestions into the form. Three rules, in order: never overwrite a
 * field the user has touched; fill a field that is blank; and replace a value an
 * earlier suggestion put there (so adding a resume re-suggests). Product
 * defaults ride along unbadged.
 */
export function applySuggestions(prefs, suggestion, { touched, replaceable }) {
  const values = { ...prefs };
  const filled = [];

  for (const [name, source] of Object.entries(suggestion.sources || {})) {
    if (touched.has(name)) continue;
    const value = suggestion.suggestions?.[name];
    if (value === undefined) continue;
    if (source === "default") {
      values[name] = value;
      continue;
    }
    if (!isBlank(values[name]) && !replaceable.includes(name)) continue;
    values[name] = value;
    filled.push(name);
  }
  return { values, filled };
}

/**
 * What the parse actually read, shown above the editable fields.
 *
 * The preferences below are the *conclusions*; this is the evidence. A resume
 * that came back with three skills and no seniority is worth seeing before you
 * trust a search built on it — and the fix (a better PDF, or an edit above) is
 * one row up.
 */
function ExtractedProfile({ profile }) {
  if (!profile?.resume_count) return null;
  const level = [
    profile.seniority,
    profile.years_experience ? `${profile.years_experience} yrs` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section className="rounded-lg border bg-ink-900/40 px-4 py-3.5 hairline">
      <p className="eyebrow">
        Read from your {profile.resume_count} resume
        {profile.resume_count === 1 ? "" : "s"}
      </p>
      <dl className="mt-2.5 grid gap-x-6 gap-y-2 sm:grid-cols-2">
        <Fact label="Experience level">{level}</Fact>
        <Fact label="Location">{profile.location}</Fact>
      </dl>
      {profile.skills?.length > 0 && (
        <div className="mt-3">
          <p className="label">Skills</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {profile.skills.slice(0, 14).map((skill) => (
              <span key={skill} className="chip">
                {skill}
              </span>
            ))}
            {profile.skills.length > 14 && (
              <span className="chip text-white/35">
                +{profile.skills.length - 14}
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function Fact({ label, children }) {
  if (!children) return null;
  return (
    <div>
      <dt className="label">{label}</dt>
      <dd className="font-mono text-xs text-white/55">{children}</dd>
    </div>
  );
}

function PrefillBanner({ fields }) {
  if (!fields.length) return null;
  const named = fields.map((name) => FIELD_LABELS[name] || name);
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-signal/25 bg-signal/[0.06] px-3.5 py-3">
      <SparkleGlyph className="mt-0.5 h-3.5 w-3.5 shrink-0 text-signal" />
      <p className="text-xs leading-relaxed text-white/55">
        <span className="text-signal">Filled in from your resumes:</span>{" "}
        {named.join(", ")}. Have a read and change anything that looks off — the rest
        is yours to fill in.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Step 3 — connect Gmail and start                                            */
/* -------------------------------------------------------------------------- */

/**
 * The mailbox and the switch, in one row. They were two steps; asking someone to
 * press "next" between granting Gmail access and turning the thing on was a step
 * that existed only to be counted.
 */
function LaunchStep({ state, status, onChange, onStarted, onError }) {
  return (
    <Step index={3} title="Connect your email and start" state={state}>
      {state === "todo" ? null : (
        <div className="space-y-5">
          <ConnectEmail status={status} onChange={onChange} onError={onError} />
          {status.gmail_connected && <RecruiterInboxToggle />}
          {status.gmail_connected && (
            <StartAutopilot
              status={status}
              onStarted={onStarted}
              onError={onError}
            />
          )}
        </div>
      )}
    </Step>
  );
}

function ConnectEmail({ status, onChange, onError }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const popupRef = useRef(null);

  // The OAuth callback posts back from the popup; a poll covers the case where
  // the user completes consent but the message is lost (blocked opener, etc).
  useEffect(() => {
    function onMessage(event) {
      if (event.origin !== window.location.origin) return;
      if (event.data?.source !== "doaide-gmail-oauth") return;
      setBusy(false);
      if (event.data.status === "connected") {
        onChange();
        toast.success("Gmail connected.");
      } else onError("Gmail connection was cancelled or failed.");
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [onChange, onError, toast]);

  useEffect(() => {
    if (!busy) return undefined;
    const timer = setInterval(async () => {
      if (popupRef.current?.closed) {
        clearInterval(timer);
        setBusy(false);
        onChange().catch(() => {});
      }
    }, 800);
    return () => clearInterval(timer);
  }, [busy, onChange]);

  async function connect() {
    onError(null);
    setBusy(true);
    try {
      const { authorization_url } = await api.gmailAuthorize();
      popupRef.current = window.open(
        authorization_url,
        "doaide-gmail",
        "width=520,height=680,noopener=no",
      );
      if (!popupRef.current) {
        setBusy(false);
        onError("Allow pop-ups for this site, then try connecting again.");
      }
    } catch (err) {
      setBusy(false);
      onError(err.message);
    }
  }

  async function disconnect(id, email) {
    if (
      !(await confirm({
        title: "Disconnect this inbox?",
        message: `${email} will stop sending and receiving for AutoApply. You can reconnect it any time.`,
        confirmLabel: "Disconnect",
        tone: "danger",
      }))
    )
      return;
    try {
      await api.gmailDisconnect(id);
      await onChange();
      toast.success("Gmail disconnected.");
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    }
  }

  if (status.gmail_connected)
    return (
      <div className="space-y-4">
        <DisconnectRow address={status.gmail_address} onDisconnect={disconnect} />
        <div>
          <button className="btn-ghost" onClick={connect} disabled={busy}>
            <GoogleGlyph />
            {busy ? "Waiting for Google…" : "Add another mailbox"}
          </button>
          <p className="mt-1.5 text-xs text-white/40">
            Adding a mailbox gives you a second identity, not a higher send
            limit — each warms up on its own.
          </p>
        </div>
      </div>
    );

  return (
    <div className="space-y-4">
      <p className="max-w-lg text-sm leading-relaxed text-white/50">
        Outreach is sent from your own Gmail, so it lands in the inbox rather
        than a spam folder — and replies come straight back to you.
      </p>
      {status.gmail_configured ? (
        <button className="btn-primary" onClick={connect} disabled={busy}>
          <GoogleGlyph />
          {busy ? "Waiting for Google…" : "Connect Gmail"}
        </button>
      ) : (
        <ErrorBanner tone="warn">
          Gmail sign-in isn't configured on this server yet. Set GOOGLE_CLIENT_ID,
          GOOGLE_CLIENT_SECRET and TOKEN_ENCRYPTION_KEY.
        </ErrorBanner>
      )}
    </div>
  );
}

function DisconnectRow({ address, onDisconnect }) {
  const [status, setStatus] = useState(null);

  const reload = useCallback(() => {
    api
      .gmailStatus()
      .then(setStatus)
      .catch(() => setStatus({ accounts: [] }));
  }, []);

  useEffect(reload, [reload, address]);

  if (!status?.accounts?.length) return null;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {status.accounts.map((account) => (
          <span key={account.id} className="chip">
            <span className="h-1.5 w-1.5 rounded-full bg-good" />
            {account.email}
            <button
              className="ml-1 text-white/30 transition-colors hover:text-bad"
              onClick={() => onDisconnect(account.id, account.email)}
              aria-label={`Disconnect ${account.email}`}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <PushToggle status={status} onChange={reload} />
    </div>
  );
}

/**
 * Gmail push: replies arrive in seconds instead of on the poll cycle.
 *
 * Framed as speed rather than as a feature to switch on, because turning it off
 * is never a loss of mail — the mailbox goes straight back to polling. The
 * failure states say so in as many words, since the alternative is a user who
 * sees "failed" and assumes replies have stopped arriving.
 */
function PushToggle({ status, onChange }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!status.push_configured) {
    return (
      <p className="text-xs text-white/30">
        Replies are checked every few minutes. For instant delivery, set
        GMAIL_PUBSUB_TOPIC on the server.
      </p>
    );
  }

  const watch = status.watch;
  const active = watch?.status === "active";

  async function toggle() {
    setBusy(true);
    setError("");
    try {
      if (active) await api.disableGmailPush();
      else await api.enableGmailPush();
      await onChange();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-quiet" onClick={toggle} disabled={busy}>
          {busy ? "Working…" : active ? "Turn off instant replies" : "Turn on instant replies"}
        </button>
        {active && <span className="chip text-good">instant</span>}
      </div>
      <p className="text-xs text-white/30">
        {active
          ? "Google tells us the moment a reply lands, so it shows up in seconds."
          : "Replies are checked every few minutes. Turning this on makes them instant."}
      </p>
      {watch?.status === "failed" && watch.last_error && (
        <p className="text-xs text-warn">
          Instant delivery couldn't start ({watch.last_error}). Your replies are
          still arriving — just on the slower check.
        </p>
      )}
      {error && <p className="text-xs text-bad">{error}</p>}
    </div>
  );
}

/**
 * Watching the inbox for recruiters who write first — and, separately, whether
 * the clearest of those may be answered without the user reading it.
 *
 * Two switches rather than one, and the inner one is deliberately harder to
 * reach. Everywhere else in DoAide AutoApply an AI reply is always reviewed before it
 * goes out; this is the single exception, so it is off by default, it names its
 * own threshold in the copy, and turning inbox watching off disarms it rather
 * than leaving it primed for a switch-on months later.
 */
function RecruiterInboxToggle() {
  const [pref, setPref] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .recruiterPreferences()
      .then(setPref)
      .catch(() => setPref({ server_enabled: false }));
  }, []);

  async function update(patch) {
    setBusy(true);
    setError("");
    try {
      setPref(await api.updateRecruiterPreferences(patch));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (!pref) return null;

  if (!pref.server_enabled) {
    return (
      <p className="text-xs text-white/30">
        Recruiter inbox watching isn't enabled on this server. Set
        RECRUITER_REPLY_ENABLED to turn it on.
      </p>
    );
  }

  return (
    <div className="space-y-3 border-t pt-4 hairline">
      <div className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="btn-quiet"
            onClick={() => update({ enabled: !pref.enabled })}
            disabled={busy}
          >
            {busy
              ? "Working…"
              : pref.enabled
                ? "Stop watching my inbox"
                : "Watch my inbox for recruiters"}
          </button>
          {pref.enabled && <span className="chip text-good">watching</span>}
        </div>
        <p className="text-xs text-white/30">
          {pref.enabled
            ? "New mail is checked every fifteen minutes. Recruiters get matched to one of your profiles and a reply is drafted for you."
            : "When a recruiter emails you out of the blue, we'll spot it among the job alerts and draft a reply against the profile that fits best."}
        </p>
      </div>

      {pref.enabled && (
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <button
              className="btn-quiet"
              onClick={() =>
                update({ auto_reply_enabled: !pref.auto_reply_enabled })
              }
              disabled={busy || !pref.server_auto_enabled}
              title={
                pref.server_auto_enabled
                  ? undefined
                  : "Automatic replies are switched off on this server"
              }
            >
              {pref.auto_reply_enabled
                ? "Stop replying automatically"
                : "Reply automatically to the clearest matches"}
            </button>
            {pref.auto_reply_enabled && (
              <span className="chip text-signal">auto-reply on</span>
            )}
          </div>
          <p className="text-xs text-white/30">
            {pref.server_auto_enabled
              ? "Only when we're over 90% sure of both the sender and the match, and never more than three a day. Everything else waits for you."
              : "This server doesn't allow replies to go out unreviewed. Every reply will wait for your approval."}
          </p>
        </div>
      )}

      {pref.last_scan_at && (
        <p className="text-xs text-white/25">
          Last check {formatWhen(pref.last_scan_at)} · {pref.detected_count}{" "}
          detected so far
        </p>
      )}
      {error && <p className="text-xs text-bad">{error}</p>}
    </div>
  );
}

function StartAutopilot({ status, onStarted, onError }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function start() {
    onError(null);
    setBusy(true);
    try {
      await api.updateAutopilot({ is_active: true });
      toast.success("Autopilot started.");
      await onStarted();
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (status.autopilot_active)
    return (
      <div className="space-y-3 border-t pt-4 hairline">
        <p className="text-sm text-white/55">
          Autopilot is running from {status.gmail_address}.
        </p>
        <button className="btn-ghost" onClick={() => onStarted()}>
          Open pipeline
        </button>
      </div>
    );

  return (
    <div className="space-y-4 border-t pt-4 hairline">
      <p className="text-sm leading-relaxed text-white/45">
        From here on it runs itself: we watch for matching jobs, score each one
        against your resumes, tailor for the good ones, find the right recruiter,
        and send from {status.gmail_address}. Sending starts gently — a few a day
        for the first week — to keep your mailbox in good standing.
      </p>
      <div className="flex items-center gap-3">
        <button className="btn-primary" onClick={start} disabled={busy}>
          {busy ? "Starting…" : "Start autopilot"}
        </button>
        <span className="text-xs text-white/30">You can pause it at any time.</span>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Optional — LinkedIn Easy Apply                                              */
/* -------------------------------------------------------------------------- */

/**
 * Deliberately outside the three steps, and deliberately blunt about the risk.
 *
 * LinkedIn's User Agreement prohibits automated access, and the account that
 * gets restricted is the candidate's own — not ours. That makes this an
 * informed choice rather than a setup step, so it sits below the numbered flow,
 * off by default at the server, and says plainly what is being traded. Anything
 * softer would be selling a risk we don't carry.
 */
function LinkedInPanel({ onError }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [status, setStatus] = useState(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  const reload = useCallback(() => {
    api
      .linkedinStatus()
      .then(setStatus)
      .catch(() => setStatus({ connected: false, enabled: false }));
  }, []);

  useEffect(reload, [reload]);

  if (!status) return null;

  async function connect(event) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.connectLinkedin(email, password);
      setPassword("");
      setOpen(false);
      reload();
      toast.success("LinkedIn connected.");
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    if (
      !(await confirm({
        title: "Disconnect LinkedIn?",
        message: "Your stored credentials are deleted. Easy Apply stops immediately.",
        confirmLabel: "Disconnect",
        tone: "danger",
      }))
    )
      return;
    try {
      await api.disconnectLinkedin();
      reload();
      toast.success("LinkedIn disconnected.");
    } catch (err) {
      onError(err.message);
    }
  }

  return (
    <section className="panel space-y-3 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className="eyebrow">Optional</p>
          <h2 className="mt-1.5 text-lg text-white">LinkedIn Easy Apply</h2>
        </div>
        {status.connected && (
          <span className="chip">
            <span className="h-1.5 w-1.5 rounded-full bg-good" />
            {status.email}
          </span>
        )}
      </div>

      <p className="max-w-lg text-sm leading-relaxed text-white/50">
        Applies to Easy Apply roles on your behalf, answering screening questions
        from your resume. Capped at {status.budget?.limit ?? 25} a day and paced
        between applications.
      </p>

      <p className="max-w-lg text-xs leading-relaxed text-warn/80">
        LinkedIn's terms prohibit automated access, and it's your account at
        risk, not ours. Your password is encrypted at rest and only ever used to
        sign in as you.
      </p>

      {!status.enabled && (
        <ErrorBanner tone="warn">
          Easy Apply is switched off on this server. Set
          LINKEDIN_EASY_APPLY_ENABLED=true to allow it.
        </ErrorBanner>
      )}

      {status.connected ? (
        <div className="flex flex-wrap items-center gap-3">
          <button className="btn-quiet hover:text-bad" onClick={disconnect}>
            Disconnect
          </button>
          <span className="font-mono text-[11px] text-white/35">
            {status.budget?.remaining ?? 0} of {status.budget?.limit ?? 25} left today
            {status.easy_apply_total ? ` · ${status.easy_apply_total} sent all-time` : ""}
          </span>
        </div>
      ) : open ? (
        <form className="max-w-sm space-y-2" onSubmit={connect}>
          <input
            className="input"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="LinkedIn email"
            aria-label="LinkedIn email"
          />
          <input
            className="input"
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="LinkedIn password"
            aria-label="LinkedIn password"
          />
          <div className="flex gap-2">
            <button className="btn-primary" type="submit" disabled={busy}>
              {busy ? "Connecting…" : "Connect"}
            </button>
            <button className="btn-quiet" type="button" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button className="btn-ghost" onClick={() => setOpen(true)}>
          Connect LinkedIn
        </button>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */

function GoogleGlyph() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" aria-hidden="true">
      <path
        fill="currentColor"
        d="M21.35 11.1H12v2.98h5.35c-.23 1.4-1.7 4.1-5.35 4.1-3.22 0-5.85-2.66-5.85-5.94S8.78 6.3 12 6.3c1.83 0 3.06.78 3.76 1.45l2.56-2.47C16.68 3.72 14.55 2.8 12 2.8 6.9 2.8 2.8 6.9 2.8 12S6.9 21.2 12 21.2c5.3 0 8.8-3.72 8.8-8.96 0-.6-.06-1.06-.15-1.5z"
      />
    </svg>
  );
}
