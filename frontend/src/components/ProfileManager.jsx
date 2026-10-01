import { useState } from "react";
import { useConfirm } from "./ui/ConfirmDialog";
import TagInput from "./ui/TagInput";
import { useToast } from "./ui/Toast";
import { api } from "../lib/api";

/**
 * Profile management — the several jobs one candidate would take.
 *
 * A resume is a document; a profile is an intent. Someone who would take a
 * backend role *or* a DevOps role has two intents, each with its own resume,
 * roles, places and salary floor, and every job that comes in is scored against
 * both. Whichever wins decides which resume gets sent.
 *
 * The screen is a list of collapsed rows rather than a form per profile: the
 * common actions — switch one off for a month, see which one is default, add a
 * new one from a resume just uploaded — are all one click from the top level,
 * and the fields are there when you open a row.
 *
 * Editing saves on blur/commit rather than behind a Save button. A profile is a
 * handful of independent fields with no cross-field validation to speak of, and
 * a button would be one more thing to forget on the way out.
 */
export default function ProfileManager({ profiles, resumes, onChange, onError }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [openId, setOpenId] = useState(null);
  const [busy, setBusy] = useState(false);

  async function run(work, success) {
    onError(null);
    setBusy(true);
    try {
      await work();
      await onChange();
      if (success) toast.success(success);
    } catch (err) {
      onError(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function addFromResume(resumeId) {
    await run(
      () => api.createProfileFromResume({ resume_id: resumeId }),
      "Profile added from your resume.",
    );
  }

  async function addBlank() {
    await run(
      () => api.createProfile({ name: "New profile", is_active: true }),
      "Profile added.",
    );
  }

  async function patch(id, payload) {
    await run(() => api.patchProfile(id, payload));
  }

  async function remove(profile) {
    if (
      !(await confirm({
        title: `Delete "${profile.name}"?`,
        message:
          "Jobs already matched to it keep their applications, but nothing new " +
          "will be scored against it. To pause it instead, switch it off.",
        confirmLabel: "Delete profile",
        tone: "danger",
      }))
    )
      return;
    await run(() => api.deleteProfile(profile.id), "Profile deleted.");
  }

  // Resumes with no profile of their own: the most useful thing this screen can
  // offer is a one-click profile built from the file the user just uploaded.
  const unused = resumes.filter(
    (resume) => !profiles.some((p) => p.resume_id === resume.id),
  );

  return (
    <div className="space-y-4">
      <p className="max-w-lg text-sm leading-relaxed text-white/50">
        A profile is one kind of job you'd take — its own resume, roles, places
        and salary floor. Every job we find is scored against all of them, and we
        apply with whichever one fits it best.
      </p>

      {profiles.length > 0 && (
        <ul className="space-y-2">
          {profiles.map((profile) => (
            <ProfileRow
              key={profile.id}
              profile={profile}
              resumes={resumes}
              open={openId === profile.id}
              busy={busy}
              onToggleOpen={() =>
                setOpenId((current) => (current === profile.id ? null : profile.id))
              }
              onPatch={(payload) => patch(profile.id, payload)}
              onRemove={() => remove(profile)}
            />
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {unused.map((resume) => (
          <button
            key={resume.id}
            className="btn-ghost"
            disabled={busy}
            onClick={() => addFromResume(resume.id)}
          >
            + Profile from {resume.headline || resume.filename || `resume #${resume.id}`}
          </button>
        ))}
        <button className="btn-quiet" disabled={busy} onClick={addBlank}>
          + Blank profile
        </button>
      </div>

      {profiles.length === 0 && (
        <p className="text-xs text-white/30">
          No profiles yet. Upload a resume above and we'll build one from it.
        </p>
      )}
    </div>
  );
}

function ProfileRow({
  profile,
  resumes,
  open,
  busy,
  onToggleOpen,
  onPatch,
  onRemove,
}) {
  const summary = [
    (profile.target_roles || []).slice(0, 2).join(", "),
    profile.remote_only
      ? "remote only"
      : (profile.location_preferences || []).slice(0, 2).join(", "),
    profile.salary_min ? `${profile.salary_min.toLocaleString()}+` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <li
      className={[
        "rounded-lg border bg-ink-700/50 hairline",
        profile.is_active ? "" : "opacity-55",
      ].join(" ")}
    >
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
        <button className="min-w-0 flex-1 text-left" onClick={onToggleOpen}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-medium text-white">
              {profile.name}
            </span>
            {profile.is_default && (
              <span className="badge bg-signal/15 text-signal">default</span>
            )}
            {!profile.is_active && (
              <span className="badge bg-white/[0.06] text-white/45">off</span>
            )}
          </div>
          <p className="mt-1 font-mono text-xs text-white/35">
            {summary || "Nothing set yet — open to fill it in."}
          </p>
          {profile.resume_label && (
            <p className="mt-0.5 font-mono text-[11px] text-white/25">
              {profile.resume_label}
            </p>
          )}
        </button>

        <div className="flex shrink-0 items-center gap-1.5">
          <button
            className="btn-quiet"
            onClick={onToggleOpen}
            aria-expanded={open}
            aria-label={open ? `Close ${profile.name}` : `Edit ${profile.name}`}
          >
            {open ? "Close" : "Edit"}
          </button>
          <button
            className="btn-quiet"
            disabled={busy}
            aria-pressed={profile.is_active}
            onClick={() => onPatch({ is_active: !profile.is_active })}
          >
            {profile.is_active ? "Switch off" : "Switch on"}
          </button>
          <button
            className="shrink-0 px-1 text-white/25 transition-colors hover:text-bad"
            disabled={busy}
            onClick={onRemove}
            aria-label={`Delete ${profile.name}`}
          >
            ×
          </button>
        </div>
      </div>

      {open && (
        <ProfileFields profile={profile} resumes={resumes} onPatch={onPatch} />
      )}
    </li>
  );
}

function ProfileFields({ profile, resumes, onPatch }) {
  const [name, setName] = useState(profile.name);

  return (
    <div className="space-y-4 border-t px-4 py-4 hairline">
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label className="label" htmlFor={`name-${profile.id}`}>
            Name
          </label>
          <input
            id={`name-${profile.id}`}
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => {
              const next = name.trim();
              // An empty name would leave the row unidentifiable in its own
              // list, so a blank edit reverts rather than saves.
              if (!next) setName(profile.name);
              else if (next !== profile.name) onPatch({ name: next });
            }}
          />
        </div>

        <div>
          <label className="label" htmlFor={`resume-${profile.id}`}>
            Resume
          </label>
          <select
            id={`resume-${profile.id}`}
            className="input"
            value={profile.resume_id ?? ""}
            onChange={(e) =>
              onPatch({
                resume_id: e.target.value ? Number(e.target.value) : null,
              })
            }
          >
            <option value="" className="bg-ink-800">
              Use my default
            </option>
            {resumes.map((resume) => (
              <option key={resume.id} value={resume.id} className="bg-ink-800">
                {resume.headline || resume.filename || `Resume #${resume.id}`}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <label className="label" htmlFor={`roles-${profile.id}`}>
          Roles
        </label>
        <TagInput
          id={`roles-${profile.id}`}
          placeholder="DevOps Engineer, SRE…"
          values={profile.target_roles || []}
          onChange={(target_roles) => onPatch({ target_roles })}
        />
      </div>

      <div>
        <label className="label" htmlFor={`locations-${profile.id}`}>
          Where you'd work
        </label>
        <TagInput
          id={`locations-${profile.id}`}
          placeholder="Berlin, London…"
          values={profile.location_preferences || []}
          onChange={(location_preferences) => onPatch({ location_preferences })}
        />
        <p className="mt-1.5 text-xs text-white/30">
          We won't auto-apply to roles outside these unless they're remote.
        </p>
      </div>

      <label className="flex cursor-pointer items-start gap-3 text-sm">
        <input
          type="checkbox"
          checked={!!profile.remote_only}
          onChange={(e) => onPatch({ remote_only: e.target.checked })}
          className="mt-0.5 h-4 w-4 shrink-0 accent-signal"
        />
        <span className="text-white/55">Remote only</span>
      </label>

      <div>
        <label className="label" htmlFor={`skills-${profile.id}`}>
          Skills to lead with
        </label>
        <TagInput
          id={`skills-${profile.id}`}
          placeholder="Kubernetes, Terraform…"
          values={profile.skills || []}
          onChange={(skills) => onPatch({ skills })}
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <NumberField
          id={`salary-min-${profile.id}`}
          label="Minimum salary"
          value={profile.salary_min}
          onCommit={(salary_min) => onPatch({ salary_min })}
        />
        <NumberField
          id={`salary-max-${profile.id}`}
          label="Up to"
          value={profile.salary_max}
          onCommit={(salary_max) => onPatch({ salary_max })}
        />
        <div>
          <label className="label" htmlFor={`level-${profile.id}`}>
            Level
          </label>
          <select
            id={`level-${profile.id}`}
            className="input"
            value={profile.experience_level ?? ""}
            onChange={(e) =>
              onPatch({ experience_level: e.target.value || null })
            }
          >
            <option value="" className="bg-ink-800">
              From my resume
            </option>
            {["junior", "mid", "senior", "lead", "exec"].map((level) => (
              <option key={level} value={level} className="bg-ink-800">
                {level}
              </option>
            ))}
          </select>
        </div>
      </div>

      {!profile.is_default && (
        <button className="btn-quiet" onClick={() => onPatch({ is_default: true })}>
          Make this my default
        </button>
      )}
    </div>
  );
}

/** A number input that commits on blur, and treats an empty field as "no answer". */
function NumberField({ id, label, value, onCommit }) {
  const [draft, setDraft] = useState(value ?? "");

  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="number"
        min={0}
        step={5000}
        className="input"
        value={draft}
        placeholder="Any"
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          const next = draft === "" ? null : Number(draft);
          if (next !== (value ?? null)) onCommit(next);
        }}
      />
    </div>
  );
}
