import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConfirmProvider } from "../components/ui/ConfirmDialog";
import { ToastProvider } from "../components/ui/Toast";
import Inbox from "./Inbox";

vi.mock("../lib/api", () => ({
  api: {
    inbox: vi.fn(),
    inboxThread: vi.fn(),
    markThreadRead: vi.fn(),
    syncInbox: vi.fn(),
    editEmail: vi.fn(),
    approveDraft: vi.fn(),
    dismissDraft: vi.fn(),
    review: vi.fn(),
    interviewPrep: vi.fn(),
    listProfiles: vi.fn(),
    recruiterInbox: vi.fn(),
    recruiterEmail: vi.fn(),
    markRecruiterEmailRead: vi.fn(),
    rematchRecruiterEmail: vi.fn(),
    generateRecruiterReply: vi.fn(),
    dismissRecruiterEmail: vi.fn(),
    scanRecruiterInbox: vi.fn(),
    recruiterStats: vi.fn(),
    emailAttachment: vi.fn(),
    emailAttachmentPreview: vi.fn(),
    emailAttachments: vi.fn(),
    setEmailResume: vi.fn(),
    addEmailAttachment: vi.fn(),
    removeEmailAttachment: vi.fn(),
  },
}));

import { api } from "../lib/api";

const THREAD = {
  thread_id: 7,
  application_id: 3,
  subject: "Backend role at Acme",
  company: "Acme",
  recruiter_name: "Dana Reed",
  recruiter_email: "talent@acme.com",
  role: "Senior Backend Engineer",
  application_status: "SCHEDULING",
  last_intent: "SCHEDULING",
  last_direction: "RECEIVED",
  snippet: "We'd love to talk — are you free Thursday?",
  message_count: 3,
  inbound_count: 1,
  outbound_count: 1,
  unread_count: 1,
  last_inbound_at: "2026-07-24T10:00:00Z",
  last_outbound_at: "2026-07-23T09:00:00Z",
  last_activity_at: "2026-07-24T10:00:00Z",
  last_message_at: "2026-07-24T10:00:00Z",
  draft_email_id: 42,
};

/** An application that has gone out and heard nothing back — outbound only. */
const SENT_ONLY_THREAD = {
  ...THREAD,
  thread_id: 9,
  company: "Initech",
  recruiter_email: "careers@initech.com",
  application_status: "OUTREACH_SENT",
  last_intent: null,
  last_direction: "SENT",
  snippet: "Hi — I'd love to be considered for the backend role.",
  message_count: 1,
  inbound_count: 0,
  outbound_count: 1,
  unread_count: 0,
  last_inbound_at: null,
  last_activity_at: "2026-07-23T09:00:00Z",
  draft_email_id: null,
};

const SECOND_THREAD = {
  ...THREAD,
  thread_id: 8,
  company: "Northwind",
  recruiter_email: "jobs@northwind.com",
  application_status: "NOT_INTERESTED",
  last_intent: "NOT_INTERESTED",
  snippet: "Unfortunately we're going to pass.",
  unread_count: 0,
  draft_email_id: null,
};

const DETAIL = {
  ...THREAD,
  messages: [
    {
      id: 40,
      direction: "SENT",
      status: "SENT",
      to_address: "talent@acme.com",
      subject: "Backend role at Acme",
      body_text: "Hi Dana — I'd love to be considered.",
      intent: null,
      is_draft: false,
      sent_at: "2026-07-23T09:00:00Z",
      created_at: "2026-07-23T09:00:00Z",
    },
    {
      id: 41,
      direction: "RECEIVED",
      status: "RECEIVED",
      from_address: "talent@acme.com",
      subject: "Re: Backend role at Acme",
      body_text: "We'd love to talk — are you free Thursday?",
      intent: "SCHEDULING",
      is_draft: false,
      sent_at: "2026-07-24T10:00:00Z",
      created_at: "2026-07-24T10:00:00Z",
    },
    {
      id: 42,
      direction: "SENT",
      status: "DRAFT",
      to_address: "talent@acme.com",
      subject: "Re: Backend role at Acme",
      body_text: "Thursday works well for me.",
      intent: null,
      is_draft: true,
      attachments: ["dana-quinn-resume.pdf"],
      attachment_note: null,
      sent_at: null,
      created_at: "2026-07-24T10:05:00Z",
    },
  ],
};

const REVIEW_ITEM = {
  email_id: 91,
  application_id: 5,
  thread_id: 11,
  kind: "outreach",
  to_address: "talent@northwind.com",
  subject: "Senior Backend Engineer — Jordan Candidate",
  body_text: "Hi — I'd love to be considered for the backend role.",
  company: "Northwind",
  recruiter_name: "Sam Ray",
  role: "Senior Backend Engineer",
  application_status: "QUEUED",
  reply_intent: null,
  incoming_snippet: null,
  created_at: "2026-07-24T09:00:00Z",
};

const PREP = {
  application_id: 3,
  company: "Acme",
  role: "Senior Backend Engineer",
  company_research: "Acme is hiring for Senior Backend Engineer based in San Francisco.",
  company_facts: ["Company: Acme", "Compensation: $160k-$200k"],
  questions: [
    { question: "How have you used FastAPI in production?", why: "Named as a requirement." },
  ],
  talking_points: [
    { point: "Lead with your Python work.", evidence: "Staff Engineer at Northwind" },
  ],
  gaps: ["Rust — the posting asks for it and your resume doesn't show it."],
  questions_to_ask: ["What does the first 90 days look like?"],
  generated_with: "llm",
};

function inboxPayload(threads = [THREAD], overrides = {}) {
  return {
    counts: {
      threads: threads.length,
      sent: threads.filter((t) => t.outbound_count > 0).length,
      received: threads.filter((t) => t.inbound_count > 0).length,
      unread: threads.filter((t) => t.unread_count > 0).length,
      awaiting_reply: threads.filter((t) => t.draft_email_id != null).length,
      // Only replied-to threads have an intent, same as the API.
      by_intent: threads.reduce(
        (acc, t) =>
          t.last_intent
            ? { ...acc, [t.last_intent]: (acc[t.last_intent] || 0) + 1 }
            : acc,
        {},
      ),
      ...overrides,
    },
    threads,
  };
}

/* ---- Recruiter Inbox fixtures ---- */

const PROFILES = [
  { id: 11, name: "Backend Engineer" },
  { id: 12, name: "Tech Lead" },
];

/** A flagged message: we weren't confident enough to answer it. */
const FLAGGED = {
  id: 101,
  from_address: "alex@northwind.com",
  from_name: "Alex Recruiter",
  subject: "Senior Backend Engineer at Northwind",
  snippet: "I came across your profile and wanted to reach out",
  received_at: "2026-07-25T10:00:00Z",
  created_at: "2026-07-25T10:00:00Z",
  kind: "RECRUITER_OUTREACH",
  classification_confidence: 0.9,
  route: "FLAG",
  route_confidence: 52,
  status: "FLAGGED",
  matched_profile_id: 11,
  matched_profile_name: "Backend Engineer",
  match_score: 58,
  match_reason: null,
  flag_reason: "Matched Backend Engineer at 58, which isn't a strong enough fit.",
  extracted: { role_title: "Senior Backend Engineer", company: "Northwind" },
  reply_email_id: null,
  application_id: null,
  read_at: null,
};

/** A drafted message: a reply is waiting in the review queue. */
const DRAFTED = {
  ...FLAGGED,
  id: 102,
  from_address: "sam@globex.com",
  from_name: "Sam Hiring",
  subject: "Staff Engineer at Globex",
  route: "DRAFT",
  route_confidence: 78,
  status: "DRAFTED",
  match_score: 82,
  flag_reason: null,
  match_reason: "Matched Backend Engineer at 82 — drafted for your review.",
  reply_email_id: 900,
  read_at: "2026-07-25T11:00:00Z",
};

const JOB_ALERT = {
  ...FLAGGED,
  id: 103,
  from_address: "jobalerts-noreply@linkedin.com",
  from_name: null,
  subject: "New jobs for you",
  kind: "JOB_ALERT",
  route: null,
  route_confidence: null,
  status: "CLASSIFIED",
  matched_profile_id: null,
  matched_profile_name: null,
  match_score: null,
  flag_reason: null,
};

const RECRUITER_DETAIL = {
  ...DRAFTED,
  body_text: "I came across your profile and wanted to reach out about a role.",
  reply_subject: "Re: Staff Engineer at Globex",
  reply_body: "Hi Sam,\n\nThanks for reaching out — I'd be glad to hear more.",
  reply_status: "DRAFT",
  reply_note: "Confirms interest and asks for the band.",
  thread_id: 55,
  reply_to_address: null,
  reply_attachments: ["jordan-quinn-resume.pdf"],
  reply_attachment_note: null,
};

function recruiterPayload(emails = [FLAGGED, DRAFTED, JOB_ALERT], overrides = {}) {
  return {
    counts: {
      detected: emails.length,
      unread: emails.filter((e) => !e.read_at).length,
      needs_you: emails.filter((e) => e.status === "FLAGGED").length,
      drafted: emails.filter((e) => e.status === "DRAFTED").length,
      replied: emails.filter((e) =>
        ["REPLIED", "REPLY_QUEUED"].includes(e.status),
      ).length,
      not_recruiter: emails.filter((e) => e.status === "CLASSIFIED").length,
      by_kind: {},
      ...overrides,
    },
    emails,
    enabled: true,
    auto_reply_enabled: false,
    server_enabled: true,
    last_scan_at: "2026-07-25T12:00:00Z",
  };
}

const STATS = {
  days: 30,
  bucket: "day",
  totals: {
    scans: 412,
    listed: 9120,
    examined: 388,
    detected: 71,
    classified_recruiter: 34,
    replied: 22,
    auto_sent: 4,
    drafts_pending: 6,
    flagged: 9,
    escalated: 2,
    dismissed: 5,
    failed: 0,
    deferred: 12,
  },
  skipped: [
    { reason: "skipped_known", label: "Already checked", count: 8602 },
    { reason: "skipped_own_thread", label: "Conversations you started", count: 402 },
  ],
  trend: [
    { period: "2026-07-21", label: "21 Jul", scanned: 1240, detected: 11, recruiter: 6, replied: 4, auto_sent: 1, flagged: 2 },
    { period: "2026-07-22", label: "22 Jul", scanned: 980, detected: 3, recruiter: 2, replied: 1, auto_sent: 0, flagged: 1 },
  ],
  push: {
    configured: true,
    healthy: true,
    covering: true,
    notifications: 318,
    last_notified_at: "2026-07-28T14:02:11Z",
    scans_from_push: 300,
    scans_from_beat: 100,
    scans_manual: 12,
  },
};

function renderInbox(path = "/inbox") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <ConfirmProvider>
          <Inbox />
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

/** What `GET /inbox/emails/{id}/attachments` returns for the draft under test. */
function attachmentsPayload(overrides = {}) {
  return {
    email_id: 42,
    editable: true,
    files: [{ index: 0, filename: "dana-quinn-resume.pdf", kind: "resume" }],
    note: null,
    resume_id: null,
    resume_removed: false,
    resume_options: [
      {
        id: 1,
        label: "Senior Backend Engineer",
        filename: "dana-backend.pdf",
        is_default: true,
        has_original_file: true,
      },
      {
        id: 2,
        label: "ML Infrastructure Engineer",
        filename: "dana-ml.pdf",
        is_default: false,
        has_original_file: true,
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.inbox.mockResolvedValue(inboxPayload());
  api.inboxThread.mockResolvedValue(DETAIL);
  api.markThreadRead.mockResolvedValue({ ...THREAD, unread_count: 0 });
  api.syncInbox.mockResolvedValue({
    threads_polled: 2,
    dispatched: false,
    errors: 0,
  });
  api.editEmail.mockResolvedValue({});
  api.approveDraft.mockResolvedValue({});
  api.dismissDraft.mockResolvedValue({});
  api.review.mockResolvedValue({ count: 0, items: [] });
  api.interviewPrep.mockResolvedValue(PREP);
  api.listProfiles.mockResolvedValue(PROFILES);
  api.recruiterInbox.mockResolvedValue(recruiterPayload());
  api.recruiterEmail.mockResolvedValue(RECRUITER_DETAIL);
  api.markRecruiterEmailRead.mockResolvedValue({});
  api.rematchRecruiterEmail.mockResolvedValue(RECRUITER_DETAIL);
  api.generateRecruiterReply.mockResolvedValue(RECRUITER_DETAIL);
  api.dismissRecruiterEmail.mockResolvedValue(undefined);
  api.scanRecruiterInbox.mockResolvedValue({
    detected: 1,
    examined: 3,
    dispatched: false,
    deferred: 0,
  });
  api.recruiterStats.mockResolvedValue(STATS);
  api.emailAttachments.mockResolvedValue(attachmentsPayload());
  api.setEmailResume.mockResolvedValue(attachmentsPayload());
  api.addEmailAttachment.mockResolvedValue(attachmentsPayload());
  api.removeEmailAttachment.mockResolvedValue(attachmentsPayload());
});

describe("Inbox", () => {
  it("lists conversations with their classification", async () => {
    renderInbox();

    expect(await screen.findByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("Senior Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText(/are you free Thursday/)).toBeInTheDocument();
    // The classified intent reads in plain language, not as the raw enum.
    expect(screen.getAllByText("scheduling").length).toBeGreaterThan(0);
  });

  it("headlines the unread count", async () => {
    renderInbox();
    expect(await screen.findByText("1 unread reply")).toBeInTheDocument();
  });

  it("flags a conversation whose draft reply is waiting", async () => {
    renderInbox();
    expect(await screen.findByText("draft ready")).toBeInTheDocument();
  });

  it("explains an empty mailbox and points at the autopilot", async () => {
    api.inbox.mockResolvedValue(inboxPayload([]));
    renderInbox();

    expect(await screen.findByText("Nothing here yet.")).toBeInTheDocument();
    expect(screen.getByText("No email yet")).toBeInTheDocument();
    // Empty because nothing has been sent — so the way out is the autopilot,
    // not another look at this page.
    expect(
      screen.getByText(/Every application sent on your behalf shows up here/),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Start the autopilot" })).toHaveAttribute(
      "href",
      "/pipeline",
    );
  });

  it("lists an application that has gone out with nothing back yet", async () => {
    api.inbox.mockResolvedValue(inboxPayload([SENT_ONLY_THREAD]));
    renderInbox();

    // The bug this covers: sent-only threads were filtered out server-side, so
    // a user who had applied fifty times saw an empty inbox.
    expect(await screen.findByText("Initech")).toBeInTheDocument();
    expect(screen.getByText(/I'd love to be considered/)).toBeInTheDocument();
    expect(screen.getByText("sent")).toBeInTheDocument();
  });

  it("headlines both halves of the mailbox", async () => {
    api.inbox.mockResolvedValue(
      inboxPayload([SENT_ONLY_THREAD], { unread: 0 }),
    );
    renderInbox();

    expect(await screen.findByText(/1 conversation · 1 sent · 0 answered/)).toBeInTheDocument();
  });

  it("opens a conversation and shows the whole thread", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));

    expect(await screen.findByText("Hi Dana — I'd love to be considered.")).toBeInTheDocument();
    expect(screen.getByText("They wrote")).toBeInTheDocument();
    expect(screen.getByText("You sent")).toBeInTheDocument();
    expect(api.inboxThread).toHaveBeenCalledWith(7);
  });

  it("badges the sent message in the conversation", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await screen.findByText("Hi Dana — I'd love to be considered.");

    // One badge on the outbound message, plus the inbound one's intent.
    expect(screen.getAllByText("sent").length).toBeGreaterThan(0);
    expect(screen.getAllByText("scheduling").length).toBeGreaterThan(0);
  });

  it("reads the conversation in the order it happened", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await screen.findByText("Hi Dana — I'd love to be considered.");

    // The API interleaves by timestamp; the pane must not reorder it.
    const history = within(screen.getByRole("list", { name: "Messages" }));
    const bodies = history.getAllByRole("listitem").map((item) => item.textContent);
    expect(bodies[0]).toContain("I'd love to be considered");
    expect(bodies[bodies.length - 1]).toContain("are you free Thursday");
  });

  it("marks a thread read when it is opened", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await waitFor(() => expect(api.markThreadRead).toHaveBeenCalledWith(7));
  });

  it("does not re-mark a thread that is already read", async () => {
    const user = userEvent.setup();
    api.inbox.mockResolvedValue(inboxPayload([SECOND_THREAD]));
    api.inboxThread.mockResolvedValue({ ...DETAIL, unread_count: 0 });
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Northwind/ }));
    await screen.findByText("Hi Dana — I'd love to be considered.");
    expect(api.markThreadRead).not.toHaveBeenCalled();
  });

  it("shows the draft reply with approve, edit and discard controls", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));

    expect(await screen.findByText("draft reply")).toBeInTheDocument();
    expect(screen.getByText("Thursday works well for me.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve & send" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
  });

  it("names the resume the conversation draft will carry", async () => {
    // The same answer the Drafts tab and the Recruiter Inbox give — a draft
    // appears in three places and should say the same thing in all of them.
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));

    expect(await screen.findByText("Attached")).toBeInTheDocument();
    expect(screen.getByText("dana-quinn-resume.pdf")).toBeInTheDocument();
  });

  it("opens the attached PDF when its name is clicked", async () => {
    // The bug: the filename was all there was. A reviewer approving a send they
    // cannot recall could not open the document it would carry.
    const user = userEvent.setup();
    URL.createObjectURL = vi.fn(() => "blob:the-resume");
    URL.revokeObjectURL = vi.fn();
    api.emailAttachment.mockResolvedValue(new Blob(["%PDF"]));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "dana-quinn-resume.pdf" }));

    // 42 is the draft; 0 is the position the name was listed at.
    await waitFor(() => expect(api.emailAttachment).toHaveBeenCalledWith(42, 0));
    const preview = await screen.findByRole("dialog", {
      name: "Preview of dana-quinn-resume.pdf",
    });
    expect(
      within(preview).getByTitle("Preview of dana-quinn-resume.pdf"),
    ).toHaveAttribute("src", "blob:the-resume");
  });

  it("opens the preview outside the conversation panel", async () => {
    // The regression this guards: `position: fixed` only resolves against the
    // viewport while no ancestor has established a containing block, and any
    // non-`none` `backdrop-filter` does — which `.panel` sets via
    // `backdrop-blur-sm`. Rendered in place, this overlay laid itself out
    // inside the conversation panel and was then clipped by that panel's
    // `overflow-hidden` and the message list's scroll box. The fetch worked,
    // the blob was fine, and the preview was a sliver nobody could see.
    const user = userEvent.setup();
    URL.createObjectURL = vi.fn(() => "blob:the-resume");
    URL.revokeObjectURL = vi.fn();
    api.emailAttachment.mockResolvedValue(new Blob(["%PDF"]));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "dana-quinn-resume.pdf" }));

    const preview = await screen.findByRole("dialog", { name: /Preview of/ });
    // Portalled: its parent is the body, not the panel it was triggered from.
    expect(preview.parentElement).toBe(document.body);
    expect(preview.closest(".panel")).toBeNull();
  });

  it("opens a PDF the recruiter sent, not just the one we would send", async () => {
    // Inbound attachments were invisible end to end: the inbox read the column
    // the *sender* writes, so a recruiter's job spec or signed offer showed
    // nothing at all on the one screen built to read it.
    const user = userEvent.setup();
    URL.createObjectURL = vi.fn(() => "blob:the-spec");
    URL.revokeObjectURL = vi.fn();
    api.emailAttachment.mockResolvedValue(new Blob(["%PDF"]));
    api.inboxThread.mockResolvedValue({
      ...DETAIL,
      messages: DETAIL.messages.map((m) =>
        m.id === 41 ? { ...m, attachments: ["role-spec.pdf", "nda.pdf"] } : m,
      ),
    });
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: /nda\.pdf/ }));

    // 41 is the received message; 1 is the position the name was listed at, so
    // the badge opens the file the server calls position 1.
    await waitFor(() => expect(api.emailAttachment).toHaveBeenCalledWith(41, 1));
    expect(
      await screen.findByRole("dialog", { name: "Preview of nda.pdf" }),
    ).toBeInTheDocument();
  });

  it("releases the document when the preview is closed", async () => {
    // A blob URL pins the file in memory until it is revoked, and a reviewer
    // opens a lot of these in a sitting.
    const user = userEvent.setup();
    URL.createObjectURL = vi.fn(() => "blob:the-resume");
    URL.revokeObjectURL = vi.fn();
    api.emailAttachment.mockResolvedValue(new Blob(["%PDF"]));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "dana-quinn-resume.pdf" }));
    await screen.findByRole("dialog", { name: /Preview of/ });
    await user.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(() =>
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:the-resume"),
    );
    expect(screen.queryByRole("dialog", { name: /Preview of/ })).not.toBeInTheDocument();
  });

  it("reads a Word document a recruiter sent, off the server's rendering", async () => {
    // A .docx job spec was as unreadable as our own: the frame drew nothing, and
    // nothing on screen is indistinguishable from a broken preview.
    const user = userEvent.setup();
    URL.createObjectURL = vi
      .fn()
      .mockReturnValueOnce("blob:the-spec")
      .mockReturnValueOnce("blob:the-rendering");
    URL.revokeObjectURL = vi.fn();
    api.emailAttachment.mockResolvedValue(new Blob(["PK"]));
    api.emailAttachmentPreview.mockResolvedValue(new Blob(["<p>the spec</p>"]));
    api.inboxThread.mockResolvedValue({
      ...DETAIL,
      messages: DETAIL.messages.map((m) =>
        m.id === 41 ? { ...m, attachments: ["role-spec.docx"] } : m,
      ),
    });
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: /role-spec\.docx/ }));

    await waitFor(() =>
      expect(api.emailAttachmentPreview).toHaveBeenCalledWith(41, 0),
    );
    const preview = await screen.findByRole("dialog", {
      name: "Preview of role-spec.docx",
    });
    expect(within(preview).getByTitle("Preview of role-spec.docx")).toHaveAttribute(
      "src",
      "blob:the-rendering",
    );
    // Download stays the file the recruiter actually sent.
    expect(within(preview).getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      "blob:the-spec",
    );
  });

  it("asks for no rendering of a PDF attachment", async () => {
    const user = userEvent.setup();
    URL.createObjectURL = vi.fn(() => "blob:the-resume");
    URL.revokeObjectURL = vi.fn();
    api.emailAttachment.mockResolvedValue(new Blob(["%PDF"]));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "dana-quinn-resume.pdf" }));

    await waitFor(() => expect(api.emailAttachment).toHaveBeenCalledWith(42, 0));
    expect(api.emailAttachmentPreview).not.toHaveBeenCalled();
  });

  it("releases the rendering as well as the document", async () => {
    // Two blobs are held open for a Word document, and both pin memory until
    // they are revoked.
    const user = userEvent.setup();
    URL.createObjectURL = vi
      .fn()
      .mockReturnValueOnce("blob:the-spec")
      .mockReturnValueOnce("blob:the-rendering");
    URL.revokeObjectURL = vi.fn();
    api.emailAttachment.mockResolvedValue(new Blob(["PK"]));
    api.emailAttachmentPreview.mockResolvedValue(new Blob(["<p>the spec</p>"]));
    api.inboxThread.mockResolvedValue({
      ...DETAIL,
      messages: DETAIL.messages.map((m) =>
        m.id === 41 ? { ...m, attachments: ["role-spec.docx"] } : m,
      ),
    });
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: /role-spec\.docx/ }));
    await screen.findByRole("dialog", { name: /Preview of/ });
    await user.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(() =>
      expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:the-rendering"),
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:the-spec");
  });

  it("says why an attachment would not open instead of showing a blank frame", async () => {
    const user = userEvent.setup();
    api.emailAttachment.mockRejectedValue(
      new Error("No resume on file — upload one and it will be attached."),
    );
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "dana-quinn-resume.pdf" }));

    expect(await screen.findByText(/No resume on file/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /Preview of/ })).not.toBeInTheDocument();
  });

  /* ---- Changing what a draft carries ---- */

  it("swaps the resume for another of the candidate's", async () => {
    // The complaint this answers: the wrong CV is queued and naming it is no
    // help unless the right one can be chosen in the same place.
    const user = userEvent.setup();
    api.setEmailResume.mockResolvedValue(
      attachmentsPayload({
        resume_id: 2,
        files: [{ index: 0, filename: "dana-ml.pdf", kind: "resume" }],
      }),
    );
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Change files" }));
    await user.selectOptions(
      await screen.findByLabelText("Send this resume"),
      "2",
    );

    await waitFor(() => expect(api.setEmailResume).toHaveBeenCalledWith(42, 2));
    expect(await screen.findByText("dana-ml.pdf")).toBeInTheDocument();
    expect(screen.queryByText("dana-quinn-resume.pdf")).not.toBeInTheDocument();
  });

  it("hands the choice back to the pipeline", async () => {
    const user = userEvent.setup();
    api.emailAttachments.mockResolvedValue(attachmentsPayload({ resume_id: 2 }));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Change files" }));
    await user.selectOptions(await screen.findByLabelText("Send this resume"), "");

    await waitFor(() => expect(api.setEmailResume).toHaveBeenCalledWith(42, null));
  });

  it("removes an attachment from the draft", async () => {
    const user = userEvent.setup();
    api.removeEmailAttachment.mockResolvedValue(
      attachmentsPayload({ files: [], resume_removed: true }),
    );
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(
      await screen.findByRole("button", { name: "Remove dana-quinn-resume.pdf" }),
    );

    // 42 is the draft; 0 is the position the file was listed at.
    await waitFor(() =>
      expect(api.removeEmailAttachment).toHaveBeenCalledWith(42, 0),
    );
    expect(await screen.findByText("Nothing attached.")).toBeInTheDocument();
  });

  it("says a removed resume can be put back rather than leaving a blank row", async () => {
    const user = userEvent.setup();
    api.emailAttachments.mockResolvedValue(
      attachmentsPayload({ files: [], resume_removed: true }),
    );
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Change files" }));

    expect(await screen.findByText(/No resume will be sent/)).toBeInTheDocument();
  });

  it("attaches a file of the user's own", async () => {
    // A recruiter asking for a portfolio used to send the user back to Gmail.
    const user = userEvent.setup();
    const file = new File(["%PDF"], "portfolio.pdf", { type: "application/pdf" });
    api.addEmailAttachment.mockResolvedValue(
      attachmentsPayload({
        files: [
          { index: 0, filename: "dana-quinn-resume.pdf", kind: "resume" },
          { index: 1, filename: "portfolio.pdf", kind: "upload", attachment_id: 5 },
        ],
      }),
    );
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Change files" }));
    await user.upload(screen.getByLabelText("Add an attachment"), file);

    await waitFor(() =>
      expect(api.addEmailAttachment).toHaveBeenCalledWith(42, file),
    );
    expect(await screen.findByText("portfolio.pdf")).toBeInTheDocument();
  });

  it("offers no controls on a message that has already been approved", async () => {
    // Changing the attachments on a queued message is a race whose loser is the
    // recruiter, who receives a document the user thought they had removed.
    const user = userEvent.setup();
    api.emailAttachments.mockResolvedValue(attachmentsPayload({ editable: false }));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await screen.findByText("dana-quinn-resume.pdf");

    expect(
      screen.queryByRole("button", { name: "Change files" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Remove / }),
    ).not.toBeInTheDocument();
  });

  it("keeps the named file on screen when the attachment list fails to load", async () => {
    // Best-effort: a failed side-load must not blank the row the thread payload
    // already answered.
    const user = userEvent.setup();
    api.emailAttachments.mockRejectedValue(new Error("network"));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));

    expect(await screen.findByText("dana-quinn-resume.pdf")).toBeInTheDocument();
  });

  it("shows what a sent message carried, in the conversation itself", async () => {
    // The API had always reported this; the thread view was the one place that
    // dropped it, so a sent application read as though it went out bare.
    const user = userEvent.setup();
    api.inboxThread.mockResolvedValue({
      ...DETAIL,
      messages: DETAIL.messages.map((m) =>
        m.id === 40 ? { ...m, attachments: ["the-one-that-went.pdf"] } : m,
      ),
    });
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));

    expect(
      await screen.findByRole("button", { name: /the-one-that-went/ }),
    ).toBeInTheDocument();
  });

  it("says so when a conversation has no draft", async () => {
    const user = userEvent.setup();
    api.inboxThread.mockResolvedValue({
      ...DETAIL,
      draft_email_id: null,
      messages: DETAIL.messages.filter((m) => !m.is_draft),
    });
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    expect(
      await screen.findByText("No reply drafted for this conversation."),
    ).toBeInTheDocument();
  });

  it("approves a draft only after the user confirms", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Approve & send" }));

    // The confirmation dialog names the recipient, and nothing has been sent yet.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/talent@acme.com/)).toBeInTheDocument();
    expect(api.approveDraft).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Approve & send" }));
    await waitFor(() => expect(api.approveDraft).toHaveBeenCalledWith(42));
  });

  it("does not send when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Approve & send" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.approveDraft).not.toHaveBeenCalled();
  });

  it("saves edits to a draft before it is sent", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Edit" }));

    const body = screen.getByLabelText("Draft body");
    await user.clear(body);
    await user.type(body, "Thursday at 2pm works.");
    await user.click(screen.getByRole("button", { name: "Save edits" }));

    await waitFor(() =>
      expect(api.editEmail).toHaveBeenCalledWith(42, {
        subject: "Re: Backend role at Acme",
        body_text: "Thursday at 2pm works.",
      }),
    );
  });

  it("discards a draft after confirmation", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Discard" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Discard draft" }));
    await waitFor(() => expect(api.dismissDraft).toHaveBeenCalledWith(42));
  });

  it("offers All, Sent and Received with their counts", async () => {
    api.inbox.mockResolvedValue(inboxPayload([THREAD, SENT_ONLY_THREAD]));
    renderInbox();

    const tabs = await screen.findAllByRole("tab");
    const labels = tabs.map((tab) => tab.textContent);
    expect(labels).toEqual(
      expect.arrayContaining(["All2", "Sent2", "Received1"]),
    );
    // All is where the page starts: the whole mailbox, not one half of it.
    expect(screen.getByRole("tab", { name: /^All/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("narrows to sent mail", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("tab", { name: /^Sent/ }));

    await waitFor(() =>
      expect(api.inbox).toHaveBeenLastCalledWith({
        direction: "sent",
        intent: "",
        unread: "",
        needs_reply: "",
        q: "",
      }),
    );
  });

  it("narrows to received mail", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("tab", { name: /^Received/ }));

    await waitFor(() =>
      expect(api.inbox).toHaveBeenLastCalledWith({
        direction: "received",
        intent: "",
        unread: "",
        needs_reply: "",
        q: "",
      }),
    );
  });

  it("clears back to the whole mailbox", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("tab", { name: /^Sent/ }));
    await waitFor(() =>
      expect(api.inbox).toHaveBeenLastCalledWith(
        expect.objectContaining({ direction: "sent" }),
      ),
    );

    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() =>
      expect(api.inbox).toHaveBeenLastCalledWith(
        expect.objectContaining({ direction: "all" }),
      ),
    );
  });

  it("filters to unread conversations", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /^Unread/ }));

    await waitFor(() =>
      expect(api.inbox).toHaveBeenLastCalledWith({
        direction: "all",
        intent: "",
        unread: "true",
        needs_reply: "",
        q: "",
      }),
    );
  });

  it("filters by classified intent", async () => {
    const user = userEvent.setup();
    api.inbox.mockResolvedValue(inboxPayload([THREAD, SECOND_THREAD]));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /^rejection/ }));

    await waitFor(() =>
      expect(api.inbox).toHaveBeenLastCalledWith({
        direction: "all",
        intent: "NOT_INTERESTED",
        unread: "",
        needs_reply: "",
        q: "",
      }),
    );
  });

  it("searches conversations", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.type(await screen.findByLabelText("Search conversations"), "acme");

    await waitFor(() =>
      expect(api.inbox).toHaveBeenLastCalledWith({
        direction: "all",
        intent: "",
        unread: "",
        needs_reply: "",
        q: "acme",
      }),
    );
  });

  it("checks for new replies on demand", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: "Check for new mail" }));

    await waitFor(() => expect(api.syncInbox).toHaveBeenCalled());
    expect(await screen.findByText("Checked 2 conversations.")).toBeInTheDocument();
  });

  it("surfaces a failure instead of failing silently", async () => {
    api.inbox.mockRejectedValue(new Error("Inbox is unavailable"));
    renderInbox();

    expect(await screen.findByText("Inbox is unavailable")).toBeInTheDocument();
  });
});

describe("Inbox drafts tab", () => {
  it("shows the drafts awaiting approval, including outreach with no thread", async () => {
    api.review.mockResolvedValue({ count: 1, items: [REVIEW_ITEM] });
    renderInbox("/inbox?tab=drafts");

    expect(await screen.findByText("1 to review")).toBeInTheDocument();
    expect(screen.getByText("outreach")).toBeInTheDocument();
    expect(screen.getByText("Northwind")).toBeInTheDocument();
    expect(
      screen.getByText("Hi — I'd love to be considered for the backend role."),
    ).toBeInTheDocument();
  });

  it("says so when nothing is waiting", async () => {
    renderInbox("/inbox?tab=drafts");
    expect(await screen.findByText("Nothing waiting.")).toBeInTheDocument();
  });

  it("names the files a draft will carry before it is approved", async () => {
    // Approving is irreversible, so the reviewer should see the attachments
    // that go with it — not discover them afterwards on the sent row.
    api.review.mockResolvedValue({
      count: 1,
      items: [{ ...REVIEW_ITEM, attachments: ["jordan-quinn-resume.pdf"] }],
    });
    api.emailAttachments.mockResolvedValue(
      attachmentsPayload({
        email_id: 91,
        files: [{ index: 0, filename: "jordan-quinn-resume.pdf", kind: "resume" }],
      }),
    );
    renderInbox("/inbox?tab=drafts");

    expect(await screen.findByText("Attached")).toBeInTheDocument();
    expect(screen.getByText("jordan-quinn-resume.pdf")).toBeInTheDocument();
  });

  it("offers a way to attach one when a draft carries nothing", async () => {
    // This row used to disappear when there was nothing to name, which was
    // right while it was only a label. Now that it is also the control for
    // adding a file, an empty draft is exactly when the user needs it.
    api.review.mockResolvedValue({ count: 1, items: [REVIEW_ITEM] });
    api.emailAttachments.mockResolvedValue(
      attachmentsPayload({ email_id: 91, files: [] }),
    );
    renderInbox("/inbox?tab=drafts");

    expect(await screen.findByText("Nothing attached.")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Change files" }),
    ).toBeInTheDocument();
  });

  it("switches between the two views", async () => {
    const user = userEvent.setup();
    api.review.mockResolvedValue({ count: 1, items: [REVIEW_ITEM] });
    renderInbox();

    await user.click(await screen.findByRole("tab", { name: /Drafts/ }));
    expect(await screen.findByText("1 to review")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Conversations/ }));
    expect(await screen.findByText("Acme")).toBeInTheDocument();
  });

  it("approves a draft from the drafts view after confirming", async () => {
    const user = userEvent.setup();
    api.review.mockResolvedValue({ count: 1, items: [REVIEW_ITEM] });
    renderInbox("/inbox?tab=drafts");

    await user.click(await screen.findByRole("button", { name: "Approve & send" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Approve & send" }));

    await waitFor(() => expect(api.approveDraft).toHaveBeenCalledWith(91));
  });

  it("re-enables the approve button when sending fails", async () => {
    const user = userEvent.setup();
    api.review.mockResolvedValue({ count: 1, items: [REVIEW_ITEM] });
    api.approveDraft.mockRejectedValue(new Error("Gmail is unreachable"));
    renderInbox("/inbox?tab=drafts");

    await user.click(await screen.findByRole("button", { name: "Approve & send" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Approve & send" }));

    // Reported in both the banner and a toast.
    expect((await screen.findAllByText("Gmail is unreachable")).length).toBeGreaterThan(0);
    // The bug this covers: without a finally the button stayed "Working…"
    // forever and a reload was the only way to try again.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Approve & send" })).toBeEnabled(),
    );
  });
});

describe("Inbox interview prep", () => {
  it("offers prep on a conversation that reached the interview stage", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    expect(await screen.findByText("Interview prep")).toBeInTheDocument();
    // Nothing is generated until asked for — it costs a model call.
    expect(api.interviewPrep).not.toHaveBeenCalled();
  });

  it("does not offer prep on a conversation that went nowhere", async () => {
    const user = userEvent.setup();
    api.inbox.mockResolvedValue(inboxPayload([SECOND_THREAD]));
    api.inboxThread.mockResolvedValue({
      ...DETAIL,
      thread_id: 8,
      application_status: "NOT_INTERESTED",
    });
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Northwind/ }));
    await screen.findByText("Hi Dana — I'd love to be considered.");
    expect(screen.queryByText("Interview prep")).not.toBeInTheDocument();
  });

  it("generates the briefing on demand", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Prepare me" }));

    await waitFor(() => expect(api.interviewPrep).toHaveBeenCalledWith(3));
    expect(await screen.findByText(/Acme is hiring for Senior Backend Engineer/)).toBeInTheDocument();
    expect(screen.getByText("How have you used FastAPI in production?")).toBeInTheDocument();
    expect(screen.getByText("Lead with your Python work.")).toBeInTheDocument();
    // The gap is the part worth reading twice, so it has to actually render.
    expect(screen.getByText(/Rust — the posting asks for it/)).toBeInTheDocument();
    expect(screen.getByText("What does the first 90 days look like?")).toBeInTheDocument();
  });

  it("surfaces a prep failure without losing the conversation", async () => {
    const user = userEvent.setup();
    api.interviewPrep.mockRejectedValue(new Error("No resume on file"));
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await user.click(await screen.findByRole("button", { name: "Prepare me" }));

    expect(await screen.findByText("No resume on file")).toBeInTheDocument();
    expect(screen.getByText("Thursday works well for me.")).toBeInTheDocument();
  });
});

describe("Inbox keyboard shortcuts", () => {
  it("moves through the list with j and k", async () => {
    const user = userEvent.setup();
    api.inbox.mockResolvedValue(inboxPayload([THREAD, SECOND_THREAD]));
    renderInbox();

    await screen.findByText("Acme");
    await user.keyboard("j");
    await waitFor(() => expect(api.inboxThread).toHaveBeenCalledWith(7));

    await user.keyboard("j");
    await waitFor(() => expect(api.inboxThread).toHaveBeenCalledWith(8));

    await user.keyboard("k");
    await waitFor(() => expect(api.inboxThread).toHaveBeenLastCalledWith(7));
  });

  it("closes the conversation with Escape", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await screen.findByText("Hi Dana — I'd love to be considered.");

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(
        screen.queryByText("Hi Dana — I'd love to be considered."),
      ).not.toBeInTheDocument(),
    );
  });

  it("opens the draft editor with r", async () => {
    const user = userEvent.setup();
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Acme/ }));
    await screen.findByText("draft reply");

    await user.keyboard("r");
    expect(await screen.findByLabelText("Draft body")).toBeInTheDocument();
  });

  it("does not fire shortcuts while the user is typing in a field", async () => {
    const user = userEvent.setup();
    renderInbox();

    const search = await screen.findByLabelText("Search conversations");
    await user.type(search, "jk");

    // Typing "jk" must search for "jk", not walk the list twice.
    expect(search).toHaveValue("jk");
    expect(api.inboxThread).not.toHaveBeenCalled();
  });
});

/* -------------------------------------------------------------------------- */
/* Recruiter Inbox                                                            */
/* -------------------------------------------------------------------------- */

describe("Recruiter Inbox", () => {
  const open = () => renderInbox("/inbox?tab=recruiters");

  it("lists detected mail with its classification and match", async () => {
    open();

    expect(await screen.findByText("Alex Recruiter")).toBeInTheDocument();
    expect(screen.getByText("Sam Hiring")).toBeInTheDocument();
    // The kind and the band read in plain language, not as raw enums.
    expect(screen.getAllByText("recruiter").length).toBeGreaterThan(0);
    expect(screen.getByText("job alert")).toBeInTheDocument();
    expect(screen.getByText("needs you")).toBeInTheDocument();
    expect(screen.getByText("drafted")).toBeInTheDocument();
  });

  it("headlines what actually needs the user", async () => {
    open();
    expect(await screen.findByText("1 message needs you")).toBeInTheDocument();
  });

  it("narrows to a single view when a filter chip is pressed", async () => {
    const user = userEvent.setup();
    open();

    await user.click(await screen.findByRole("button", { name: /Needs you/ }));

    expect(api.recruiterInbox).toHaveBeenCalledWith({ view: "needs_you" });
  });

  it("shows why a message was flagged, and offers to write a reply", async () => {
    const user = userEvent.setup();
    api.recruiterEmail.mockResolvedValue({
      ...FLAGGED,
      body_text: "I came across your profile.",
      reply_subject: null,
      reply_body: null,
      reply_status: null,
    });
    open();

    await user.click(await screen.findByRole("button", { name: /Alex Recruiter/ }));

    expect(await screen.findByText(/isn't a strong enough fit/)).toBeInTheDocument();
    // A flagged message offers to write one — and offers no way to send.
    expect(screen.getByRole("button", { name: "Write a reply" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Approve & send/ }),
    ).not.toBeInTheDocument();
  });

  it("renders a drafted reply with the ordinary approve controls", async () => {
    const user = userEvent.setup();
    open();

    await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));

    expect(await screen.findByText(/I'd be glad to hear more/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Approve & send" }),
    ).toBeInTheDocument();
  });

  it("names the resume the drafted reply will carry", async () => {
    // Attachments resolve at send time, so a draft used to show nothing at all —
    // making "the resume is queued" and "there is no resume" identical on screen.
    const user = userEvent.setup();
    api.emailAttachments.mockResolvedValue(
      attachmentsPayload({
        email_id: 900,
        files: [{ index: 0, filename: "jordan-quinn-resume.pdf", kind: "resume" }],
      }),
    );
    open();

    await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));

    expect(await screen.findByText("Attached")).toBeInTheDocument();
    expect(screen.getByText("jordan-quinn-resume.pdf")).toBeInTheDocument();
  });

  it("says why no resume is queued instead of showing an empty row", async () => {
    const user = userEvent.setup();
    api.recruiterEmail.mockResolvedValue({
      ...RECRUITER_DETAIL,
      reply_attachments: [],
      reply_attachment_note: "No resume on file — upload one and it will be attached.",
    });
    api.emailAttachments.mockResolvedValue(
      attachmentsPayload({
        email_id: 900,
        files: [],
        note: "No resume on file — upload one and it will be attached.",
      }),
    );
    open();

    await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));

    expect(await screen.findByText(/No resume on file/)).toBeInTheDocument();
  });

  it("addresses the reply to the Reply-To when the sender named one", async () => {
    // Platform mail comes from an unmonitored address; the confirmation dialog
    // has to name the address that will actually receive the answer.
    const user = userEvent.setup();
    api.recruiterEmail.mockResolvedValue({
      ...RECRUITER_DETAIL,
      from_address: "noreply@gem.example.com",
      reply_to_address: "sam@globex.com",
    });
    open();

    await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));
    await user.click(await screen.findByRole("button", { name: "Approve & send" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/sam@globex.com/)).toBeInTheDocument();
  });

  it("approves a drafted reply through the shared review endpoint", async () => {
    const user = userEvent.setup();
    open();

    await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));
    await user.click(await screen.findByRole("button", { name: "Approve & send" }));

    // Same confirmation as everywhere else, naming the actual recipient.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/sam@globex.com/)).toBeInTheDocument();
    expect(api.approveDraft).not.toHaveBeenCalled();

    await user.click(
      within(dialog).getByRole("button", { name: "Approve & send" }),
    );
    // The same endpoint the Conversations and Drafts tabs use — one draft, one
    // code path, whichever tab you reached it from.
    await waitFor(() => expect(api.approveDraft).toHaveBeenCalledWith(900));
  });

  it("marks a message read when it is opened", async () => {
    const user = userEvent.setup();
    api.recruiterEmail.mockResolvedValue({
      ...FLAGGED,
      body_text: "I came across your profile.",
      reply_status: null,
    });
    open();

    await user.click(await screen.findByRole("button", { name: /Alex Recruiter/ }));

    await waitFor(() =>
      expect(api.markRecruiterEmailRead).toHaveBeenCalledWith(101),
    );
  });

  it("rematches when the user picks a different profile", async () => {
    const user = userEvent.setup();
    open();

    await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));
    const picker = await screen.findByLabelText("Matched profile");
    await user.selectOptions(picker, "12");

    expect(api.rematchRecruiterEmail).toHaveBeenCalledWith(102, {
      profile_id: 12,
    });
  });

  it("explains itself when watching is switched off", async () => {
    api.recruiterInbox.mockResolvedValue({
      ...recruiterPayload([]),
      enabled: false,
    });
    open();

    expect(await screen.findByText("Inbox watching is off")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Turn it on in setup/ }),
    ).toBeInTheDocument();
  });

  it("says so when the server has not enabled the feature at all", async () => {
    api.recruiterInbox.mockResolvedValue({
      ...recruiterPayload([]),
      enabled: false,
      server_enabled: false,
    });
    open();

    expect(await screen.findByText(/isn't enabled on this server/)).toBeInTheDocument();
  });

  it("distinguishes an empty mailbox from a narrow filter", async () => {
    api.recruiterInbox.mockResolvedValue(recruiterPayload([]));
    open();

    expect(await screen.findByText("Nothing detected yet")).toBeInTheDocument();
  });

  it("reports what a capped scan left behind", async () => {
    const user = userEvent.setup();
    api.scanRecruiterInbox.mockResolvedValue({
      detected: 2,
      examined: 25,
      dispatched: false,
      deferred: 12,
    });
    open();

    await user.click(await screen.findByRole("button", { name: "Check now" }));

    expect(
      await screen.findByText(/12 left for the next check/),
    ).toBeInTheDocument();
  });

  it("shows the reason and a retry when the first load fails", async () => {
    api.recruiterInbox.mockRejectedValue(new Error("upstream is down"));
    open();

    expect(await screen.findByText("upstream is down")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  describe("follow-ups", () => {
    const ESCALATED = {
      ...DRAFTED,
      id: 104,
      status: "REPLIED",
      route: "AUTO",
      escalated: true,
      escalation_reason:
        "You replied to sam@globex.com on 12 July and they've written again.",
      follow_up_count: 1,
    };

    it("marks a recruiter who wrote back, even though the status says replied", async () => {
      api.recruiterInbox.mockResolvedValue(
        recruiterPayload([ESCALATED], { needs_you: 1, escalated: 1 }),
      );
      open();

      expect(await screen.findByText("wrote back")).toBeInTheDocument();
    });

    it("explains the escalation above the message", async () => {
      const user = userEvent.setup();
      api.recruiterInbox.mockResolvedValue(
        recruiterPayload([ESCALATED], { needs_you: 1, escalated: 1 }),
      );
      api.recruiterEmail.mockResolvedValue({ ...RECRUITER_DETAIL, ...ESCALATED });
      open();

      await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));

      expect(
        await screen.findByText(/they've written again/),
      ).toBeInTheDocument();
    });

    it("does not print the escalation sentence twice", async () => {
      const user = userEvent.setup();
      api.recruiterInbox.mockResolvedValue(
        recruiterPayload([ESCALATED], { needs_you: 1, escalated: 1 }),
      );
      // The pipeline sets flag_reason to the escalation reason — it is the same
      // fact, and the banner above the message already carries it.
      api.recruiterEmail.mockResolvedValue({
        ...RECRUITER_DETAIL,
        ...ESCALATED,
        flag_reason: ESCALATED.escalation_reason,
        match_reason: "Matched Backend Engineer at 91.",
      });
      open();

      await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));

      expect(
        await screen.findAllByText(/they've written again/),
      ).toHaveLength(1);
      // And the panel still says what it is for.
      expect(screen.getByText("Matched Backend Engineer at 91.")).toBeInTheDocument();
    });

    it("still shows an ordinary flag reason", async () => {
      const user = userEvent.setup();
      api.recruiterEmail.mockResolvedValue({
        ...FLAGGED,
        body_text: "I came across your profile.",
        reply_status: null,
      });
      open();

      await user.click(await screen.findByRole("button", { name: /Alex Recruiter/ }));

      expect(
        await screen.findByText(/isn't a strong enough fit/),
      ).toBeInTheDocument();
    });

    it("leaves an ordinary message unmarked", async () => {
      open();

      expect(await screen.findByText("Sam Hiring")).toBeInTheDocument();
      expect(screen.queryByText("wrote back")).not.toBeInTheDocument();
    });
  });

  describe("the resume a reply will carry", () => {
    it("says why that document was chosen", async () => {
      const user = userEvent.setup();
      api.recruiterEmail.mockResolvedValue({
        ...RECRUITER_DETAIL,
        resume_choice_reason:
          "Matched this role at 81, against 64 for your default resume.",
      });
      open();

      await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));

      expect(
        await screen.findByText(/against 64 for your default resume/),
      ).toBeInTheDocument();
    });

    it("says nothing when the ordinary resolution applied", async () => {
      const user = userEvent.setup();
      open();

      await user.click(await screen.findByRole("button", { name: /Sam Hiring/ }));

      expect(await screen.findByText(/They wrote/)).toBeInTheDocument();
      expect(screen.queryByText(/against .* for your default/)).not.toBeInTheDocument();
    });
  });

  describe("activity", () => {
    it("is collapsed until asked for", async () => {
      open();

      expect(
        await screen.findByRole("button", { name: "Activity" }),
      ).toBeInTheDocument();
      expect(api.recruiterStats).not.toHaveBeenCalled();
    });

    it("loads and shows the headline numbers when opened", async () => {
      const user = userEvent.setup();
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));

      expect(await screen.findByText("9,120")).toBeInTheDocument();
      expect(screen.getByText("Scanned")).toBeInTheDocument();
      expect(screen.getByText("Sent for you")).toBeInTheDocument();
    });

    it("names the skip reasons in English", async () => {
      const user = userEvent.setup();
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));

      expect(await screen.findByText("Already checked")).toBeInTheDocument();
      expect(screen.getByText("8,602")).toBeInTheDocument();
      // The raw counter name never reaches the page.
      expect(screen.queryByText("skipped_known")).not.toBeInTheDocument();
    });

    it("refetches when the range changes", async () => {
      const user = userEvent.setup();
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));
      await screen.findByText("Scanned");
      await user.click(screen.getByRole("button", { name: "90 days" }));

      await waitFor(() =>
        expect(api.recruiterStats).toHaveBeenCalledWith({ days: 90, bucket: "week" }),
      );
    });

    it("hides again when toggled off", async () => {
      const user = userEvent.setup();
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));
      await screen.findByText("Scanned");
      await user.click(screen.getByRole("button", { name: "Hide activity" }));

      expect(screen.queryByText("Scanned")).not.toBeInTheDocument();
    });

    it("says push is doing the work when it is", async () => {
      const user = userEvent.setup();
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));

      expect(
        await screen.findByText(/notifying us as mail arrives/),
      ).toBeInTheDocument();
    });

    it("says so when push is registered but has gone quiet", async () => {
      const user = userEvent.setup();
      api.recruiterStats.mockResolvedValue({
        ...STATS,
        push: { ...STATS.push, covering: false, healthy: true },
      });
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));

      expect(
        await screen.findByText(/registered but has been quiet/),
      ).toBeInTheDocument();
    });

    it("calls out recruiters who wrote back", async () => {
      const user = userEvent.setup();
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));

      expect(
        await screen.findByText(/2 recruiters have written back/),
      ).toBeInTheDocument();
    });

    it("says what a capped scan left rather than implying full coverage", async () => {
      const user = userEvent.setup();
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));

      expect(
        await screen.findByText(/12 left for the next check/),
      ).toBeInTheDocument();
    });

    it("surfaces a failed stats read without breaking the tab", async () => {
      const user = userEvent.setup();
      api.recruiterStats.mockRejectedValue(new Error("stats are down"));
      open();

      await user.click(await screen.findByRole("button", { name: "Activity" }));

      expect(await screen.findByText("stats are down")).toBeInTheDocument();
      // The messages are still there — the panel is a footnote, not the page.
      expect(screen.getByText("Sam Hiring")).toBeInTheDocument();
    });
  });
});
