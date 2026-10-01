// API client for the TalentPing backend.
// Stores the JWT in localStorage and attaches it as a Bearer token.

const BASE = "/api/v1";
const TOKEN_KEY = "talentping_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function request(path, { method = "GET", body, form, auth = true } = {}) {
  const headers = {};
  const token = getToken();
  if (auth && token) headers["Authorization"] = `Bearer ${token}`;

  let payload;
  if (form) {
    payload = form; // URLSearchParams or FormData
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  const res = await fetch(`${BASE}${path}`, { method, headers, body: payload });

  if (res.status === 401) setToken(null);

  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail ?? res.statusText;
    // FastAPI validation errors arrive as a list of {loc, msg} objects.
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg).join(", ")
      : typeof detail === "string"
        ? detail
        : JSON.stringify(detail);
    throw new Error(message);
  }
  return data;
}

/** Fetch an endpoint that returns text (downloads), not JSON. */
async function requestText(path) {
  const headers = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${BASE}${path}`, { headers });
  if (res.status === 401) setToken(null);
  const text = await res.text();
  if (!res.ok) throw new Error(text || res.statusText);
  return text;
}

/**
 * Fetch an endpoint that returns binary (a rendered PDF), not JSON or text.
 *
 * The error path still reads the body as text, because a failure here is a
 * FastAPI JSON error rather than a document — a 409 on a run with no stored PDF
 * should surface its "use the Markdown download" message, not a broken file.
 */
async function requestBlob(path) {
  const headers = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${BASE}${path}`, { headers });
  if (res.status === 401) setToken(null);
  if (!res.ok) {
    const text = await res.text();
    let message = text || res.statusText;
    try {
      message = JSON.parse(text)?.detail ?? message;
    } catch {
      // A non-JSON error body is already the best message we have.
    }
    throw new Error(message);
  }
  return res.blob();
}

export const api = {
  // ---- auth ----
  register: (email, password, full_name) =>
    request("/auth/register", {
      method: "POST",
      auth: false,
      body: { email, password, full_name },
    }),
  login: async (email, password) => {
    const form = new URLSearchParams({ username: email, password });
    const data = await request("/auth/login", { method: "POST", auth: false, form });
    setToken(data.access_token);
    return data;
  },
  me: () => request("/auth/me"),
  logout: () => setToken(null),

  // ---- onboarding / gmail ----
  onboarding: () => request("/onboarding"),
  gmailStatus: () => request("/gmail/status"),
  gmailAuthorize: () => request("/gmail/authorize"),
  gmailDisconnect: (id) => request(`/gmail/accounts/${id}`, { method: "DELETE" }),
  // Push notifications: replies land in seconds instead of on the poll cycle.
  // Turning it off is never a loss of mail — the mailbox goes back to polling.
  enableGmailPush: () => request("/gmail/watch", { method: "POST" }),
  disableGmailPush: () => request("/gmail/watch", { method: "DELETE" }),

  // ---- resumes ----
  listResumes: () => request("/resumes"),
  uploadResumes: (files) => {
    const fd = new FormData();
    for (const file of files) fd.append("files", file);
    return request("/resumes", { method: "POST", form: fd });
  },
  patchResume: (id, payload) =>
    request(`/resumes/${id}`, { method: "PATCH", body: payload }),
  // The document itself, so a resume can be read before it is sent anywhere: the
  // candidate's upload when we kept it, else the PDF the sender would render.
  // Bytes, because the endpoint is behind a bearer token an <iframe src> on the
  // API path would not carry.
  resumeFile: (id) => requestBlob(`/resumes/${id}/file`),
  // The same document rendered into something a browser will draw — HTML for a
  // Word file, the PDF itself for a PDF. Only ever framed: `resumeFile` is what
  // a download hands over and what a recruiter receives, and a rendering that
  // could be saved in its place would undo that.
  resumePreview: (id) => requestBlob(`/resumes/${id}/preview`),
  // Preference values this resume implies, for pre-filling the setup wizard.
  suggestedPreferences: (id) => request(`/resumes/${id}/suggested-preferences`),
  // The same, merged across every resume the user has uploaded — what setup
  // pre-fills from, so a second resume widens the search instead of being
  // ignored in favour of the default.
  mergedSuggestedPreferences: () => request("/resumes/suggested-preferences"),
  deleteResume: (id) => request(`/resumes/${id}`, { method: "DELETE" }),

  // ---- profiles ----
  // One profile per kind of job the candidate would take: a resume plus the
  // roles, places, pay and level that go with it. Jobs are scored against every
  // active one and applied to under whichever wins.
  listProfiles: () => request("/profiles"),
  createProfile: (payload) => request("/profiles", { method: "POST", body: payload }),
  // Build one from a resume's parse rather than asking the user to retype what
  // the file already says.
  createProfileFromResume: (payload) =>
    request("/profiles/from-resume", { method: "POST", body: payload }),
  patchProfile: (id, payload) =>
    request(`/profiles/${id}`, { method: "PATCH", body: payload }),
  deleteProfile: (id) => request(`/profiles/${id}`, { method: "DELETE" }),

  // ---- campaigns ----
  listCampaigns: () => request("/campaigns"),
  createCampaign: (payload) => request("/campaigns", { method: "POST", body: payload }),
  getCampaign: (id) => request(`/campaigns/${id}`),
  pauseCampaign: (id) => request(`/campaigns/${id}/pause`, { method: "POST" }),
  resumeCampaign: (id) => request(`/campaigns/${id}/resume`, { method: "POST" }),

  // ---- tracker ----
  tracker: (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return request(`/tracker${qs ? `?${qs}` : ""}`);
  },
  outreach: (id) => request(`/tracker/${id}`),
  editEmail: (id, payload) =>
    request(`/tracker/emails/${id}`, { method: "PATCH", body: payload }),

  // ---- recruiters ----
  listRecruiters: () => request("/recruiters"),
  discover: (companies) =>
    request("/recruiters/discover", { method: "POST", body: { companies } }),

  // ---- smart apply: tailoring + fit scoring ----
  tailor: (payload) => request("/tailor", { method: "POST", body: payload }),
  listTailored: () => request("/tailor"),
  getTailored: (id) => request(`/tailor/${id}`),
  // Downloads return text, not JSON — the caller turns it into a Blob.
  downloadTailored: (id, doc = "resume") =>
    requestText(`/tailor/${id}/download?doc=${doc}`),
  // The PDF is what gets attached to an application, so it comes back as bytes
  // rather than text — the caller saves the Blob directly.
  downloadTailoredPdf: (id) => requestBlob(`/tailor/${id}/pdf`),
  fitScore: (payload) => request("/fit-score", { method: "POST", body: payload }),

  // ---- cover letters ----
  writeCoverLetter: (payload) =>
    request("/cover-letter", { method: "POST", body: payload }),
  listCoverLetters: () => request("/cover-letter"),
  getCoverLetter: (id) => request(`/cover-letter/${id}`),
  editCoverLetter: (id, body) =>
    request(`/cover-letter/${id}`, { method: "PATCH", body: { body } }),
  downloadCoverLetter: (id) => requestText(`/cover-letter/${id}/download`),

  // ---- job feed ----
  listJobs: (params = {}) => request(`/jobs${qs(params)}`),
  getJob: (id) => request(`/jobs/${id}`),
  // Salary band + company research for one posting. A separate call because it
  // costs nothing on a hundred-row feed nobody expanded, and both halves are
  // cached globally — the first candidate to open a card pays for the research.
  jobIntel: (id, { refresh = false } = {}) =>
    request(`/jobs/${id}/intel${refresh ? "?refresh=true" : ""}`),
  patchJob: (id, payload) => request(`/jobs/${id}`, { method: "PATCH", body: payload }),
  deleteJob: (id) => request(`/jobs/${id}`, { method: "DELETE" }),
  jobProviders: () => request("/jobs/providers/status"),

  // Triage a whole selection: save | apply | dismiss | archive.
  bulkJobs: (job_ids, action) =>
    request("/jobs/bulk", { method: "POST", body: { job_ids, action } }),

  listSearches: () => request("/jobs/searches"),
  createSearch: (payload) => request("/jobs/searches", { method: "POST", body: payload }),
  patchSearch: (id, payload) =>
    request(`/jobs/searches/${id}`, { method: "PATCH", body: payload }),
  deleteSearch: (id) => request(`/jobs/searches/${id}`, { method: "DELETE" }),
  runSearch: (id) => request(`/jobs/searches/${id}/run`, { method: "POST" }),

  // ---- dashboard ----
  dashboard: (params = {}) => request(`/dashboard${qs(params)}`),
  dashboardQueue: () => request("/dashboard/queue"),
  // CSV comes back as text; the caller turns it into a Blob to download.
  exportApplicationsCsv: (params = {}) =>
    requestText(`/dashboard/export.csv${qs(params)}`),

  // ---- analytics ----
  analytics: (params = {}) => request(`/analytics/overview${qs(params)}`),
  subjectVariants: (params = {}) => request(`/analytics/subject-variants${qs(params)}`),
  bounceReport: (params = {}) => request(`/analytics/bounces${qs(params)}`),

  // ---- autopilot ----
  getAutopilot: () => request("/autopilot"),
  updateAutopilot: (payload) => request("/autopilot", { method: "PUT", body: payload }),
  runAutopilot: () => request("/autopilot/run", { method: "POST" }),
  autopilotReputation: () => request("/autopilot/reputation"),

  // ---- inbox: recruiter conversations ----
  inbox: (params = {}) => request(`/inbox${qs(params)}`),
  inboxThread: (id) => request(`/inbox/threads/${id}`),
  markThreadRead: (id) => request(`/inbox/threads/${id}/read`, { method: "POST" }),
  // The file behind an attachment badge. Bytes, not JSON — the caller turns it
  // into an object URL so the PDF can be read before the reply goes out.
  emailAttachment: (emailId, index) =>
    requestBlob(`/inbox/emails/${emailId}/attachments/${index}`),
  // The same file rendered for reading, for the formats a browser won't draw —
  // a recruiter's .docx job spec is as unreadable in a frame as our own.
  emailAttachmentPreview: (emailId, index) =>
    requestBlob(`/inbox/emails/${emailId}/attachments/${index}/preview`),
  // What a draft will carry, and everything it could carry instead. Every one
  // of the four returns the same shape, so the compose UI re-renders off the
  // response rather than reloading the whole thread after each change.
  emailAttachments: (emailId) => request(`/inbox/emails/${emailId}/attachments`),
  // Pin a specific resume, or pass null to hand the choice back to the pipeline.
  setEmailResume: (emailId, resumeId) =>
    request(`/inbox/emails/${emailId}/attachments`, {
      method: "PATCH",
      body: { resume_id: resumeId },
    }),
  addEmailAttachment: (emailId, file) => {
    const fd = new FormData();
    fd.append("file", file);
    return request(`/inbox/emails/${emailId}/attachments`, {
      method: "POST",
      form: fd,
    });
  },
  removeEmailAttachment: (emailId, index) =>
    request(`/inbox/emails/${emailId}/attachments/${index}`, { method: "DELETE" }),
  syncInbox: () => request("/inbox/sync", { method: "POST" }),

  // ---- recruiter inbox: mail we didn't start ----
  // Acting on a generated reply deliberately goes through the review endpoints
  // below, not through here — one draft, one code path.
  recruiterInbox: (params = {}) => request(`/recruiter-inbox${qs(params)}`),
  // How much mail was scanned, what came of it, and how that moved over time.
  // `days` is the window, `bucket` is "day" or "week".
  recruiterStats: (params = {}) => request(`/recruiter-inbox/stats${qs(params)}`),
  recruiterEmail: (id) => request(`/recruiter-inbox/${id}`),
  recruiterPreferences: () => request("/recruiter-inbox/preferences"),
  updateRecruiterPreferences: (payload) =>
    request("/recruiter-inbox/preferences", { method: "PATCH", body: payload }),
  scanRecruiterInbox: () => request("/recruiter-inbox/scan", { method: "POST" }),
  markRecruiterEmailRead: (id) =>
    request(`/recruiter-inbox/${id}/read`, { method: "POST" }),
  rematchRecruiterEmail: (id, payload = {}) =>
    request(`/recruiter-inbox/${id}/rematch`, { method: "POST", body: payload }),
  generateRecruiterReply: (id) =>
    request(`/recruiter-inbox/${id}/generate-reply`, { method: "POST" }),
  dismissRecruiterEmail: (id) =>
    request(`/recruiter-inbox/${id}/dismiss`, { method: "POST" }),

  // ---- review queue ----
  review: () => request("/review"),
  approveDraft: (id) => request(`/review/emails/${id}/approve`, { method: "POST" }),
  dismissDraft: (id) => request(`/review/emails/${id}/dismiss`, { method: "POST" }),

  // ---- interview prep ----
  interviewPrep: (applicationId) =>
    request(`/interview-prep/${applicationId}`, { method: "POST" }),

  // ---- career-page form auto-apply ----
  applyViaForm: (jobId, submit = false) =>
    request(`/jobs/${jobId}/apply-form?submit=${submit}`, { method: "POST" }),

  // ---- LinkedIn ----
  linkedinStatus: () => request("/linkedin/status"),
  connectLinkedin: (email, password) =>
    request("/linkedin/credentials", { method: "PUT", body: { email, password } }),
  disconnectLinkedin: () => request("/linkedin/credentials", { method: "DELETE" }),
  linkedinEasyApply: (jobId) =>
    request(`/linkedin/easy-apply/${jobId}`, { method: "POST" }),

  // ---- ATS form applications (Workday / Greenhouse / Lever) ----
  formApplyStatus: () => request("/form-apply/status"),
  getFormApplyProfile: () => request("/form-apply/profile"),
  updateFormApplyProfile: (payload) =>
    request("/form-apply/profile", { method: "PUT", body: payload }),
  listFormApplications: (params = {}) => request(`/form-apply${qs(params)}`),
  getFormApplication: (id) => request(`/form-apply/${id}`),
  formApplyToJob: (jobId, payload = {}) =>
    request(`/form-apply/jobs/${jobId}`, { method: "POST", body: payload }),
  retryFormApplication: (id) =>
    request(`/form-apply/${id}/retry`, { method: "POST" }),
};

/** Build a query string, dropping empty values so `?company=` never appears. */
function qs(params) {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== null && v !== undefined && v !== "",
  );
  return entries.length ? `?${new URLSearchParams(entries)}` : "";
}
