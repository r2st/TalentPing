/**
 * Auto-apply actions on a job card: the ATS form filler, and LinkedIn Easy Apply.
 *
 * Both drive a real browser against a real employer's form, so the interaction
 * is built around one rule: **filling is not submitting.** Filling costs the
 * candidate nothing and is reversible — the run stops with the form populated
 * and screenshots taken, and they look before anything goes. Submitting is the
 * irreversible half and always asks first, every time, because an application
 * sent to the wrong employer cannot be recalled and there is no undo to offer.
 *
 * The platform label is shown rather than hidden. "Apply via Greenhouse" tells
 * a candidate what the automation is about to drive; "Auto-apply" tells them
 * nothing and is exactly the affordance people click by accident.
 */
import { useState } from "react";

import { useConfirm } from "./ui/ConfirmDialog";
import { useToast } from "./ui/Toast";
import { api } from "../lib/api";

/** URL patterns the backend's ATS detector recognises, mirrored for labelling. */
const ATS_HOSTS = [
  [/myworkdayjobs\.com/i, "Workday"],
  [/greenhouse\.io|boards\.greenhouse/i, "Greenhouse"],
  [/jobs\.lever\.co/i, "Lever"],
];

/** The ATS this posting's URL points at, or null if we don't recognise it. */
export function detectAts(url) {
  if (!url) return null;
  return ATS_HOSTS.find(([pattern]) => pattern.test(url))?.[1] ?? null;
}

export function isLinkedInJob(url) {
  return Boolean(url && /linkedin\.com\/jobs/i.test(url));
}

export default function ApplyActions({ job, onError }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);

  const ats = detectAts(job.url);
  const linkedin = isLinkedInJob(job.url);
  if (!ats && !linkedin) return null;

  async function run(fn, { submit, what }) {
    // Submitting is the irreversible half, so it asks every time. Filling
    // doesn't: the run stops with the form populated for the user to check.
    if (
      submit &&
      !(await confirm({
        title: `Submit this application?`,
        message: `It will be submitted to ${job.company || "the employer"} through ${what}. This can't be recalled.`,
        confirmLabel: "Fill in & submit",
      }))
    )
      return;

    setBusy(true);
    try {
      const result = await fn();
      toast.success(
        submit
          ? `Submitting through ${what} — watch the Pipeline for the outcome.`
          : `Filling the ${what} form. We'll stop before submitting so you can check it.`,
      );
      return result;
    } catch (err) {
      onError?.(err.message);
      toast.error(err.message);
    } finally {
      setBusy(false);
    }
  }

  if (linkedin) {
    return (
      <button
        className="btn-quiet"
        disabled={busy}
        onClick={() => run(() => api.linkedinEasyApply(job.id), { submit: true, what: "LinkedIn Easy Apply" })}
      >
        {busy ? "Applying…" : "Easy Apply"}
      </button>
    );
  }

  return (
    <>
      <button
        className="btn-quiet"
        disabled={busy}
        onClick={() => run(() => api.formApplyToJob(job.id, { submit: false }), { submit: false, what: ats })}
      >
        {busy ? "Filling…" : `Fill via ${ats}`}
      </button>
      <button
        className="btn-quiet"
        disabled={busy}
        onClick={() => run(() => api.formApplyToJob(job.id, { submit: true }), { submit: true, what: ats })}
      >
        Submit
      </button>
    </>
  );
}
