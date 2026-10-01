import { useState } from "react";
import { INDUSTRIES } from "../lib/constants";
import Slider from "./ui/Slider";
import TagInput from "./ui/TagInput";

/**
 * The one preferences field set, shared by the setup wizard (step 3) and the
 * autopilot page. It renders the thirteen fields the agent runs on; how those
 * changes are persisted is the caller's business:
 *
 *   • Autopilot passes an `onChange` that saves to PUT /autopilot immediately
 *     (sliders debounce their own commits).
 *   • Setup passes an `onChange` that updates local state and saves on a button.
 *
 * `suggested` / `notes` drive the setup wizard's "filled in from your resume"
 * affordance; autopilot just omits them.
 */
export default function PreferencesForm({
  prefs,
  onChange,
  resumes = [],
  suggested = [],
  notes = {},
  sliderDebounce = 500,
}) {
  const set = (patch) => onChange(patch);
  const isSuggested = (name) => suggested.includes(name);

  return (
    <div className="space-y-5">
      <div>
        <FieldLabel
          htmlFor="roles"
          text="Roles you want"
          suggested={isSuggested("target_roles")}
          note={notes.target_roles}
        />
        <TagInput
          id="roles"
          placeholder="Staff Engineer, Backend Lead…"
          values={prefs.target_roles || []}
          onChange={(target_roles) => set({ target_roles })}
          suggested={isSuggested("target_roles")}
        />
        {!isSuggested("target_roles") && (
          <p className="mt-1.5 text-xs text-white/25">
            Leave empty and we'll infer them from your resume.
          </p>
        )}
      </div>

      <div>
        <FieldLabel
          htmlFor="locations"
          text="Where"
          suggested={isSuggested("locations")}
          note={notes.locations}
        />
        <TagInput
          id="locations"
          placeholder="San Francisco, New York…"
          values={prefs.locations || []}
          onChange={(locations) => set({ locations })}
          suggested={isSuggested("locations")}
        />
      </div>

      <Check
        checked={prefs.remote_only}
        onChange={(remote_only) => set({ remote_only })}
        label={
          <>
            Remote only
            {isSuggested("remote_only") && <SuggestedBadge note={notes.remote_only} />}
          </>
        }
      />

      <div>
        <FieldLabel
          text="Industries"
          suggested={isSuggested("target_industries")}
          note={notes.target_industries}
        />
        <div className="flex flex-wrap gap-1.5">
          {industryChoices(prefs.target_industries).map((industry) => {
            const on = (prefs.target_industries || []).includes(industry);
            return (
              <button
                key={industry}
                onClick={() =>
                  set({
                    target_industries: on
                      ? prefs.target_industries.filter((i) => i !== industry)
                      : [...(prefs.target_industries || []), industry],
                  })
                }
                className={[
                  "rounded-md border px-2.5 py-1 font-mono text-[11px] transition-colors",
                  on
                    ? "border-signal/50 bg-signal/15 text-signal"
                    : "border-white/10 text-white/45 hover:border-white/25 hover:text-white/80",
                ].join(" ")}
              >
                {industry}
              </button>
            );
          })}
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <FieldLabel
            htmlFor="salary"
            text="Minimum salary"
            suggested={isSuggested("salary_min")}
            note={notes.salary_min}
          />
          <input
            id="salary"
            type="number"
            min={0}
            step={5000}
            className={["input", isSuggested("salary_min") && "input-suggested"]
              .filter(Boolean)
              .join(" ")}
            value={prefs.salary_min ?? ""}
            onChange={(e) =>
              set({ salary_min: e.target.value ? Number(e.target.value) : null })
            }
            placeholder="Any"
          />
          {isSuggested("salary_min") && notes.salary_min && (
            <p className="mt-1.5 text-xs text-white/25">{notes.salary_min}</p>
          )}
        </div>

        {resumes.length > 1 && (
          <div>
            <label className="label" htmlFor="resume">
              Use resume
            </label>
            <select
              id="resume"
              className="input"
              value={prefs.resume_id ?? resumes.find((r) => r.is_default)?.id ?? ""}
              onChange={(e) => set({ resume_id: Number(e.target.value) })}
            >
              {resumes.map((resume) => (
                <option key={resume.id} value={resume.id} className="bg-ink-800">
                  {resume.headline || resume.filename || `Resume #${resume.id}`}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      <div>
        <label className="label" htmlFor="limit">
          Up to {prefs.daily_application_limit} applications a day
        </label>
        <Slider
          id="limit"
          aria-label="Applications per day"
          min={1}
          max={30}
          step={1}
          value={prefs.daily_application_limit}
          onCommit={(daily_application_limit) => set({ daily_application_limit })}
          debounce={sliderDebounce}
          format={(v) => `${v}/day`}
        />
        <p className="mt-1 text-xs text-white/30">
          Warm-up caps this lower for the first few weeks to protect your inbox.
        </p>
      </div>

      <div>
        <label className="label" htmlFor="fit">
          Only apply above a fit score of {prefs.min_fit_score}
        </label>
        <Slider
          id="fit"
          aria-label="Minimum fit score"
          min={40}
          max={95}
          step={5}
          value={prefs.min_fit_score}
          onCommit={(min_fit_score) => set({ min_fit_score })}
          debounce={sliderDebounce}
          format={(v) => `${v}%`}
        />
        <p className="mt-1 text-xs text-white/30">
          {notes.min_fit_score || "Higher means fewer, better-matched applications."}
        </p>
      </div>

      <Check
        checked={prefs.auto_send}
        onChange={(auto_send) => set({ auto_send })}
        label="Send automatically"
        hint="Spaced out over hours so it never looks like a blast. Uncheck to review each email first. Replies are always drafted for review either way."
      />

      <Check
        checked={prefs.form_autofill_enabled}
        onChange={(form_autofill_enabled) => set({ form_autofill_enabled })}
        label="Also fill in company application forms"
        hint="Best-effort. We fill name, email, phone and attach your resume, and stop if anything looks unfamiliar."
      />

      <Check
        checked={prefs.cover_letter_enabled}
        onChange={(cover_letter_enabled) => set({ cover_letter_enabled })}
        label="Write a cover letter for each application"
        hint="Grounded in your resume and what we actually know about the company. Anything it can't support gets dropped."
      />

      {prefs.cover_letter_enabled && (
        <div className="ml-6 space-y-1.5">
          <span className="label">How it travels</span>
          <div className="flex flex-wrap gap-2">
            {[
              ["inline", "In the email body"],
              ["attachment", "As an attachment"],
            ].map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={[
                  "chip",
                  prefs.cover_letter_delivery === value
                    ? "border-signal/40 text-signal"
                    : "text-white/50",
                ].join(" ")}
                aria-pressed={prefs.cover_letter_delivery === value}
                onClick={() => set({ cover_letter_delivery: value })}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="text-xs text-white/30">
            Inline is safer: a cold email carrying an attachment from an unknown
            sender is materially likelier to be filtered.
          </p>
        </div>
      )}

      <FollowUpSettings
        count={prefs.follow_up_count}
        days={prefs.follow_up_interval_days}
        stopOnReply={prefs.follow_up_stop_on_reply}
        onCount={(follow_up_count) => set({ follow_up_count })}
        onDays={(follow_up_interval_days) => set({ follow_up_interval_days })}
        onStopOnReply={(follow_up_stop_on_reply) => set({ follow_up_stop_on_reply })}
      />
    </div>
  );
}

/** The list of fields autopilot writes on save — used to build the PUT body. */
export const PREFERENCE_FIELDS = [
  "resume_id",
  "target_roles",
  "target_industries",
  "locations",
  "remote_only",
  "salary_min",
  "min_fit_score",
  "daily_application_limit",
  "auto_send",
  // The trial ramp rides along with the toggle. Leaving it out of this list is
  // what made the "you approve the first few by hand" promise inert: the wizard
  // received the suggestion, showed the note, then saved everything except this,
  // so the column kept its default of 0 and auto-send began on email one.
  "auto_send_trial_approvals",
  "form_autofill_enabled",
  "cover_letter_enabled",
  "cover_letter_delivery",
  "follow_up_count",
  "follow_up_interval_days",
  "follow_up_stop_on_reply",
];

/** Pick just the persisted preference fields out of a form-state object. */
export function preferencePayload(prefs) {
  const out = {};
  for (const field of PREFERENCE_FIELDS) out[field] = prefs[field];
  return out;
}

/* ---- small building blocks ------------------------------------------------ */

function Check({ checked, onChange, label, hint }) {
  return (
    <label className="flex cursor-pointer items-start gap-3 text-sm">
      <input
        type="checkbox"
        checked={!!checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 shrink-0 accent-signal"
      />
      <span className="text-white/55">
        {label}
        {hint && <span className="block text-xs text-white/30">{hint}</span>}
      </span>
    </label>
  );
}

/** The fixed chip list, plus any suggested industry outside our vocabulary. */
function industryChoices(selected) {
  const extra = (selected || []).filter((name) => !INDUSTRIES.includes(name));
  return [...INDUSTRIES, ...extra];
}

/** Marks a value as coming from the resume rather than from the user. */
export function SuggestedBadge({ note }) {
  return (
    <span
      className="ml-2 inline-flex items-center gap-1 align-middle font-mono text-[10px] uppercase tracking-wider text-signal/75"
      title={note || "Suggested from your resume"}
    >
      <SparkleGlyph className="h-2.5 w-2.5" />
      from resume
    </span>
  );
}

export function FieldLabel({ text, htmlFor, suggested, note }) {
  return (
    <label className="label" htmlFor={htmlFor}>
      {text}
      {suggested && <SuggestedBadge note={note} />}
    </label>
  );
}

/** Four-point sparkle — the mark for "we worked this out from your resume". */
export function SparkleGlyph({ className }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true" fill="currentColor">
      <path d="M11 2.5l1.7 5.3 5.3 1.7-5.3 1.7L11 16.5 9.3 11.2 4 9.5l5.3-1.7z" />
      <path d="M18.5 14l.8 2.3 2.2.7-2.2.7-.8 2.3-.8-2.3-2.2-.7 2.2-.7z" opacity=".55" />
    </svg>
  );
}

/**
 * Follow-up sequence configuration — collapsed by default because the defaults
 * come from response-rate research and most people should leave them alone.
 */
function FollowUpSettings({ count, days, stopOnReply, onCount, onDays, onStopOnReply }) {
  const [open, setOpen] = useState(false);

  // Mirrors the widening cadence the scheduler uses on the server.
  const schedule = Array.from({ length: count }, (_, index) => {
    const step = index + 1;
    return days * step + index * Math.floor(days / 2);
  });

  return (
    <div className="rounded-lg border bg-ink-900/40 hairline">
      <button
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="text-sm text-white/55">
          Follow-ups
          <span className="block text-xs text-white/30">
            {count === 0
              ? "Off — one email only."
              : `${count} follow-up${count === 1 ? "" : "s"}, around ${
                  schedule.length ? `day ${schedule.join(" and day ")}` : ""
                }.`}
          </span>
        </span>
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-white/30">
          {open ? "close" : "adjust"}
        </span>
      </button>

      {open && (
        <div className="space-y-4 border-t px-4 py-4 hairline">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="label" htmlFor="fu-count">
                How many
              </label>
              <select
                id="fu-count"
                className="input"
                value={count}
                onChange={(e) => onCount(Number(e.target.value))}
              >
                {[0, 1, 2, 3, 4, 5].map((n) => (
                  <option key={n} value={n} className="bg-ink-800">
                    {n === 0 ? "None" : `${n} follow-up${n === 1 ? "" : "s"}`}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="label" htmlFor="fu-days">
                First one after
              </label>
              <select
                id="fu-days"
                className="input"
                value={days}
                onChange={(e) => onDays(Number(e.target.value))}
                disabled={count === 0}
              >
                {[3, 4, 5, 7, 10].map((n) => (
                  <option key={n} value={n} className="bg-ink-800">
                    {n} days
                  </option>
                ))}
              </select>
            </div>
          </div>

          <label className="flex cursor-pointer items-start gap-3 text-sm">
            <input
              type="checkbox"
              checked={stopOnReply}
              onChange={(e) => onStopOnReply(e.target.checked)}
              disabled={count === 0}
              className="mt-0.5 h-4 w-4 shrink-0 accent-signal"
            />
            <span className="text-white/55">
              Stop as soon as they reply
              <span className="block text-xs text-white/30">
                Strongly recommended. Chasing someone who already wrote back is the
                fastest way to lose them.
              </span>
            </span>
          </label>

          <p className="text-xs leading-relaxed text-white/25">
            Follow-ups go out Tuesday to Thursday morning, when replies are roughly
            four times more likely than an evening send.
          </p>
        </div>
      )}
    </div>
  );
}
