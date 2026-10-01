// Shared constants used across pages. Kept here so the setup wizard, autopilot
// and the shared preferences form can't drift out of sync.

/** The industry vocabulary offered as toggle chips wherever targeting is set. */
export const INDUSTRIES = [
  "tech",
  "fintech",
  "ai",
  "healthcare",
  "ecommerce",
  "security",
  "data",
  "gaming",
];

/** How each application/pipeline status reads and colours in a table cell. */
export const APPLICATION_STATUS_STYLE = {
  QUEUED: ["queued", "bg-white/[0.06] text-white/45"],
  OUTREACH_SENT: ["sent", "bg-sky-400/15 text-sky-300"],
  FOLLOW_UP: ["follow-up", "bg-sky-400/15 text-sky-300"],
  REPLIED: ["replied", "bg-signal/15 text-signal"],
  INTERESTED: ["interested", "bg-good/15 text-good"],
  SCHEDULING: ["scheduling", "bg-good/15 text-good"],
  INTERVIEW_SCHEDULED: ["interview", "bg-good/20 text-good"],
  NOT_INTERESTED: ["passed", "bg-white/[0.06] text-white/35"],
  NO_RESPONSE: ["no reply", "bg-white/[0.06] text-white/35"],
  UNSUBSCRIBED: ["opted out", "bg-bad/10 text-bad/70"],
  CLOSED: ["closed", "bg-white/[0.06] text-white/35"],
};

/**
 * How each classified reply intent reads and colours in the inbox. Ordered the
 * way the filter chips are offered: the intents worth acting on come first,
 * the noise (auto-replies, opt-outs) last.
 */
export const REPLY_INTENT_STYLE = {
  OFFER: ["offer", "bg-good/20 text-good"],
  INTERESTED: ["interested", "bg-good/15 text-good"],
  SCHEDULING: ["scheduling", "bg-good/15 text-good"],
  QUESTION: ["question", "bg-signal/15 text-signal"],
  NOT_INTERESTED: ["rejection", "bg-bad/10 text-bad/70"],
  OUT_OF_OFFICE: ["auto-reply", "bg-white/[0.06] text-white/35"],
  UNSUBSCRIBE: ["opted out", "bg-bad/10 text-bad/70"],
  OTHER: ["other", "bg-white/[0.06] text-white/45"],
};

/** The intent order used for the inbox filter chips. */
export const REPLY_INTENTS = Object.keys(REPLY_INTENT_STYLE);

/**
 * The brief Scout wrote a draft to. Distinct from the intent above: the intent
 * describes the recruiter's last message, the template describes what the reply
 * is trying to do. They come apart exactly where it matters — a below-market
 * offer is classified OFFER and answered as a negotiation.
 */
export const REPLY_TEMPLATE_LABEL = {
  INTERESTED: "interest",
  SCHEDULING: "scheduling",
  SALARY_NEGOTIATION: "negotiation",
  DECLINING: "declining",
  FOLLOW_UP: "follow-up",
  QUESTION: "answering",
};

export const REPLY_TEMPLATE_STYLE = {
  // The negotiation draft is the one worth spotting from across the page: it is
  // the only template that changes what the candidate walks away with.
  SALARY_NEGOTIATION: "bg-signal/20 text-signal",
  INTERESTED: "bg-good/15 text-good",
  SCHEDULING: "bg-good/15 text-good",
  QUESTION: "bg-sky-400/15 text-sky-300",
  FOLLOW_UP: "bg-white/[0.06] text-white/45",
  DECLINING: "bg-white/[0.06] text-white/35",
};

/**
 * What the classifier decided an inbound message is. Only the first two are ever
 * replied to; the rest are recorded so the counts are honest about what the
 * filter threw away.
 */
export const RECRUITER_KIND_STYLE = {
  RECRUITER_OUTREACH: ["recruiter", "bg-good/15 text-good"],
  HIRING_MANAGER: ["hiring manager", "bg-good/20 text-good"],
  JOB_ALERT: ["job alert", "bg-white/[0.06] text-white/35"],
  ATS_AUTOMATED: ["automated", "bg-white/[0.06] text-white/35"],
  NOT_RECRUITER: ["not a recruiter", "bg-white/[0.06] text-white/35"],
  UNKNOWN: ["unclear", "bg-warn/15 text-warn"],
};

/**
 * Which confidence band fired. This is the decision, and it is deliberately
 * legible: "replied for you" is a different promise from "drafted", and a user
 * should never have to guess which one happened.
 */
export const REPLY_ROUTE_STYLE = {
  AUTO: ["replied for you", "bg-signal/20 text-signal"],
  DRAFT: ["drafted", "bg-signal/15 text-signal"],
  FLAG: ["needs you", "bg-warn/15 text-warn"],
};

/** Where an inbound message has got to. */
export const RECRUITER_STATUS_STYLE = {
  DETECTED: ["checking", "bg-white/[0.06] text-white/45"],
  CLASSIFIED: ["filed", "bg-white/[0.06] text-white/35"],
  FLAGGED: ["needs you", "bg-warn/15 text-warn"],
  DRAFTED: ["draft ready", "bg-signal/15 text-signal"],
  REPLY_QUEUED: ["sending", "bg-sky-400/15 text-sky-300"],
  REPLIED: ["replied", "bg-good/15 text-good"],
  IGNORED: ["dismissed", "bg-white/[0.06] text-white/35"],
  FAILED: ["failed", "bg-bad/10 text-bad/70"],
};

/**
 * Statuses where a real person is now involved and the candidate has to
 * perform. These are what surface the interview prep panel — INTERESTED counts
 * because the time to prepare is when they say yes, not when the invite lands.
 */
export const INTERVIEW_STAGE_STATUSES = new Set([
  "INTERESTED",
  "SCHEDULING",
  "INTERVIEW_SCHEDULED",
  "OFFER",
]);

/** Campaign statuses that are still moving, so the tracker keeps polling. */
export const LIVE_CAMPAIGN_STATUSES = new Set([
  "DISCOVERING",
  "GENERATING",
  "ACTIVE",
]);
