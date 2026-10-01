import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConfirmProvider } from "../components/ui/ConfirmDialog";
import { ToastProvider } from "../components/ui/Toast";
import Setup, { applySuggestions, previewFilename, stepState } from "./Setup";

vi.mock("../lib/api", () => ({
  api: {
    onboarding: vi.fn(),
    listResumes: vi.fn(),
    uploadResumes: vi.fn(),
    deleteResume: vi.fn(),
    patchResume: vi.fn(),
    resumeFile: vi.fn(),
    resumePreview: vi.fn(),
    listProfiles: vi.fn(),
    createProfile: vi.fn(),
    createProfileFromResume: vi.fn(),
    patchProfile: vi.fn(),
    deleteProfile: vi.fn(),
    mergedSuggestedPreferences: vi.fn(),
    getAutopilot: vi.fn(),
    updateAutopilot: vi.fn(),
    gmailAuthorize: vi.fn(),
    gmailStatus: vi.fn(),
    gmailDisconnect: vi.fn(),
    enableGmailPush: vi.fn(),
    disableGmailPush: vi.fn(),
    recruiterPreferences: vi.fn(),
    updateRecruiterPreferences: vi.fn(),
    linkedinStatus: vi.fn(),
    connectLinkedin: vi.fn(),
    disconnectLinkedin: vi.fn(),
  },
}));

const navigate = vi.fn();
vi.mock("react-router-dom", async () => ({
  ...(await vi.importActual("react-router-dom")),
  useNavigate: () => navigate,
}));

import { api } from "../lib/api";

const ONBOARDING = {
  gmail_configured: true,
  gmail_connected: false,
  gmail_address: null,
  resume_count: 0,
  campaign_count: 0,
  sent_count: 0,
  autopilot_configured: false,
  autopilot_active: false,
  next_step: "upload_resume",
  complete: false,
};

const BACKEND_RESUME = {
  id: 1,
  filename: "backend.pdf",
  full_name: "Jordan Candidate",
  headline: "Staff Backend Engineer",
  location: "San Francisco, CA",
  years_experience: 10,
  seniority: "lead",
  skills: ["python", "kafka"],
  is_default: true,
  has_original_file: true,
  file_content_type: "application/pdf",
  file_size: 84_000,
};

const PLATFORM_RESUME = {
  id: 2,
  filename: "platform.pdf",
  full_name: "Jordan Candidate",
  headline: "Platform Engineer",
  location: "Austin, TX",
  years_experience: 8,
  seniority: "senior",
  skills: ["kubernetes"],
  is_default: false,
  has_original_file: true,
  file_content_type: "application/pdf",
  file_size: 61_000,
};

/** The same resume as a Word upload — the format a browser will not draw. */
const wordResume = () => ({
  ...BACKEND_RESUME,
  filename: "backend.docx",
  file_content_type:
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
});

const PREFS = {
  resume_id: null,
  target_roles: [],
  target_industries: [],
  locations: [],
  remote_only: false,
  salary_min: null,
  min_fit_score: 70,
  daily_application_limit: 10,
  auto_send: true,
  form_autofill_enabled: false,
  cover_letter_enabled: true,
  cover_letter_delivery: "inline",
  follow_up_count: 2,
  follow_up_interval_days: 4,
  follow_up_stop_on_reply: true,
  configured_at: null,
};

const SUGGESTIONS = {
  resume_id: null,
  suggestions: {
    target_roles: ["Staff Backend Engineer", "Platform Engineer"],
    target_industries: ["fintech"],
    locations: ["San Francisco, CA", "Austin, TX"],
    remote_only: false,
    salary_min: 175000,
    min_fit_score: 60,
    daily_application_limit: 5,
    auto_send: true,
    auto_send_trial_approvals: 3,
  },
  sources: {
    target_roles: "resume",
    target_industries: "resume",
    locations: "resume",
    salary_min: "resume",
    min_fit_score: "default",
    daily_application_limit: "default",
    auto_send: "default",
    auto_send_trial_approvals: "default",
  },
  notes: {
    target_roles: "From the titles across your resumes.",
    target_industries: "Industries your resumes point at.",
    locations: "Where your resumes say you are.",
    salary_min: "The lowest expectation stated across your resumes.",
    min_fit_score: "A good starting point.",
    daily_application_limit: "Starts small.",
    auto_send: "Off to start.",
  },
  prefilled_fields: ["target_roles", "target_industries", "locations", "salary_min"],
  profile: {
    resume_ids: [1, 2],
    resume_count: 2,
    full_name: "Jordan Candidate",
    location: "San Francisco, CA",
    seniority: "lead",
    years_experience: 10,
    skills: ["python", "kafka", "kubernetes"],
    titles: ["Staff Backend Engineer", "Platform Engineer"],
  },
};

/** The tag chips a TagInput is currently showing, by its field label. */
function tagsUnder(labelPattern) {
  const input = screen.getByLabelText(labelPattern);
  return within(input.parentElement.parentElement);
}

/** Drop files straight on the hidden input, bypassing its `accept` filter —
 *  browsers do the same on drag-and-drop, so the component must filter itself. */
function dropFiles(input, files) {
  fireEvent.change(input, { target: { files } });
}

function renderSetup() {
  return render(
    <MemoryRouter initialEntries={["/setup"]}>
      <ToastProvider>
        <ConfirmProvider>
          <Setup />
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.onboarding.mockResolvedValue(ONBOARDING);
  api.listResumes.mockResolvedValue([]);
  api.getAutopilot.mockResolvedValue({ ...PREFS });
  api.updateAutopilot.mockResolvedValue({ ...PREFS });
  api.mergedSuggestedPreferences.mockResolvedValue(SUGGESTIONS);
  api.recruiterPreferences.mockResolvedValue({
    enabled: false,
    auto_reply_enabled: false,
    server_enabled: true,
    server_auto_enabled: false,
    last_scan_at: null,
    detected_count: 0,
    replied_count: 0,
  });
  api.updateRecruiterPreferences.mockResolvedValue({
    enabled: true,
    auto_reply_enabled: false,
    server_enabled: true,
    server_auto_enabled: false,
    last_scan_at: null,
    detected_count: 0,
    replied_count: 0,
  });
  api.uploadResumes.mockResolvedValue([BACKEND_RESUME]);
  api.deleteResume.mockResolvedValue(null);
  api.patchResume.mockResolvedValue(BACKEND_RESUME);
  api.resumeFile.mockResolvedValue(new Blob(["%PDF"]));
  api.resumePreview.mockResolvedValue(new Blob(["<p>a rendering</p>"]));
  URL.createObjectURL = vi.fn(() => "blob:the-resume");
  URL.revokeObjectURL = vi.fn();
  api.listProfiles.mockResolvedValue([]);
  api.createProfile.mockResolvedValue({});
  api.createProfileFromResume.mockResolvedValue({});
  api.patchProfile.mockResolvedValue({});
  api.deleteProfile.mockResolvedValue(null);
  api.gmailStatus.mockResolvedValue({ accounts: [], push_configured: false });
  api.linkedinStatus.mockResolvedValue({ connected: false, enabled: false, budget: {} });
});

describe("Setup — the page renders at all", () => {
  it("shows the three steps in resume-first order", async () => {
    renderSetup();

    await screen.findByRole("heading", { name: "Add your resumes" });
    const headings = screen.getAllByRole("heading", { level: 2 });
    expect(headings.slice(0, 3).map((h) => h.textContent)).toEqual([
      "Add your resumes",
      "Confirm your search",
      "Connect your email and start",
    ]);
    // The optional LinkedIn panel sits below the numbered flow, never inside it.
    expect(
      await screen.findByRole("heading", { name: "LinkedIn Easy Apply" }),
    ).toBeInTheDocument();
  });

  it("says why it could not load instead of showing a skeleton forever", async () => {
    api.onboarding.mockRejectedValue(new Error("Service unavailable"));
    renderSetup();

    expect(await screen.findByText("We couldn't load your setup.")).toBeInTheDocument();
    expect(screen.getByText("Service unavailable")).toBeInTheDocument();
  });

  it("a failed resume read is just as visible as a failed onboarding read", async () => {
    api.listResumes.mockRejectedValue(new Error("Resumes are down"));
    renderSetup();

    expect(await screen.findByText("Resumes are down")).toBeInTheDocument();
  });

  it("retries the load on demand", async () => {
    const user = userEvent.setup();
    api.onboarding.mockRejectedValueOnce(new Error("offline"));
    renderSetup();

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(
      await screen.findByRole("heading", { name: "Add your resumes" }),
    ).toBeInTheDocument();
  });

  it("stays usable once setup is complete", async () => {
    api.onboarding.mockResolvedValue({
      ...ONBOARDING,
      gmail_connected: true,
      gmail_address: "candidate@gmail.com",
      resume_count: 1,
      autopilot_configured: true,
      autopilot_active: true,
      next_step: "done",
      complete: true,
    });
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
    renderSetup();

    expect(await screen.findByText("You're all set.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Add your resumes" })).toBeInTheDocument();
  });
});

describe("Setup — step 1: resumes", () => {
  it("accepts several PDFs in one drop", async () => {
    const user = userEvent.setup();
    renderSetup();

    const input = await screen.findByLabelText("Resume files");
    await user.upload(input, [
      new File(["a"], "backend.pdf", { type: "application/pdf" }),
      new File(["b"], "platform.pdf", { type: "application/pdf" }),
    ]);

    await waitFor(() => expect(api.uploadResumes).toHaveBeenCalled());
    const sent = api.uploadResumes.mock.calls[0][0];
    expect(sent.map((f) => f.name)).toEqual(["backend.pdf", "platform.pdf"]);
  });

  it("accepts a Word .docx — the file most candidates actually have", async () => {
    // A resume lives in Word until the moment it is sent. Refusing the .docx
    // sitting in Documents sent the user off to export a PDF before the product
    // would talk to them, and it was refused three times over: the picker's
    // accept list, this filter, and a 415 from the server.
    const user = userEvent.setup();
    renderSetup();

    await user.upload(await screen.findByLabelText("Resume files"), [
      new File(["x"], "resume.docx", {
        type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      }),
    ]);

    await waitFor(() => expect(api.uploadResumes).toHaveBeenCalled());
    expect(api.uploadResumes.mock.calls[0][0].map((f) => f.name)).toEqual([
      "resume.docx",
    ]);
  });

  it("offers the fix when the file is a legacy .doc", async () => {
    renderSetup();

    dropFiles(await screen.findByLabelText("Resume files"), [
      new File(["x"], "resume.doc", { type: "application/msword" }),
    ]);

    expect(await screen.findByText(/Save As/)).toBeInTheDocument();
    expect(api.uploadResumes).not.toHaveBeenCalled();
  });

  it("rejects a file that is neither, without calling the server", async () => {
    renderSetup();

    dropFiles(await screen.findByLabelText("Resume files"), [
      new File(["x"], "resume.pages", { type: "application/x-iwork-pages-sffpages" }),
    ]);

    expect(
      await screen.findByText("Resumes need to be a PDF or a Word .docx."),
    ).toBeInTheDocument();
    expect(api.uploadResumes).not.toHaveBeenCalled();
  });

  it("keeps the unreadable files out of a mixed selection", async () => {
    renderSetup();

    dropFiles(await screen.findByLabelText("Resume files"), [
      new File(["a"], "backend.pdf", { type: "application/pdf" }),
      new File(["b"], "notes.txt", { type: "text/plain" }),
    ]);

    await waitFor(() => expect(api.uploadResumes).toHaveBeenCalled());
    expect(api.uploadResumes.mock.calls[0][0].map((f) => f.name)).toEqual([
      "backend.pdf",
    ]);
  });

  it("lists every uploaded resume with what was read off it", async () => {
    api.listResumes.mockResolvedValue([BACKEND_RESUME, PLATFORM_RESUME]);
    renderSetup();

    expect(await screen.findByText("Staff Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText("Platform Engineer")).toBeInTheDocument();
    expect(screen.getByText("2 resumes")).toBeInTheDocument();
    // The filename leads: it is the only field that differs when a candidate
    // uploads two variants of one CV, which both parse to the same headline.
    expect(
      screen.getByText(
        "backend.pdf · Jordan Candidate · 10 yrs · lead · San Francisco, CA",
      ),
    ).toBeInTheDocument();
  });

  it("surfaces an upload failure rather than swallowing it", async () => {
    api.uploadResumes.mockRejectedValue(new Error("no text found — scanned PDF"));
    renderSetup();

    dropFiles(await screen.findByLabelText("Resume files"), [
      new File(["a"], "scan.pdf", { type: "application/pdf" }),
    ]);

    // Once in the page banner, once as a toast.
    expect(
      await screen.findAllByText("no text found — scanned PDF"),
    ).not.toHaveLength(0);
  });

  it("promotes a resume to default", async () => {
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME, PLATFORM_RESUME]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Make Platform Engineer the default" }),
    );

    expect(api.patchResume).toHaveBeenCalledWith(2, { is_default: true });
  });

  it("says the default changed, rather than changing it silently", async () => {
    // Two resumes parsed to the same headline made a silent success
    // indistinguishable from a dead button — which is what it was reported as.
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME, PLATFORM_RESUME]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Make Platform Engineer the default" }),
    );

    expect(
      await screen.findByText("Platform Engineer is now your default resume."),
    ).toBeInTheDocument();
  });

  it("names each resume's buttons, so two identical headlines stay distinguishable", async () => {
    // Every row shows the same words ("Make default", "Delete"), so the
    // accessible name is the only thing telling a screen reader which row it is
    // on — and duplicate headlines are exactly when that matters.
    api.listResumes.mockResolvedValue([BACKEND_RESUME, PLATFORM_RESUME]);
    renderSetup();

    expect(
      await screen.findByRole("button", { name: "Delete Staff Backend Engineer" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Delete Platform Engineer" }),
    ).toBeInTheDocument();
  });

  it("shows the filename, the only thing telling two same-headline resumes apart", async () => {
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
    renderSetup();

    expect(await screen.findByText(/backend\.pdf/)).toBeInTheDocument();
  });

  it("offers no way to re-default the resume that already is one", async () => {
    api.listResumes.mockResolvedValue([BACKEND_RESUME, PLATFORM_RESUME]);
    renderSetup();

    await screen.findByText("2 resumes");
    expect(
      screen.queryByRole("button", { name: "Make Staff Backend Engineer the default" }),
    ).not.toBeInTheDocument();
  });

  it("confirms before deleting a resume", async () => {
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Delete Staff Backend Engineer" }),
    );
    expect(await screen.findByText("Delete this resume?")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete resume" }));
    expect(api.deleteResume).toHaveBeenCalledWith(1);
  });

  it("opens the resume itself, not another description of it", async () => {
    // The gap this closes: setup took a file and then only ever described it
    // back. "Make this one the default" and "delete this one" are both decisions
    // about a document the page gave you no way to look at.
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Preview Staff Backend Engineer" }),
    );

    await waitFor(() => expect(api.resumeFile).toHaveBeenCalledWith(1));
    const preview = await screen.findByRole("dialog", {
      name: "Preview of backend.pdf",
    });
    // The PDF renders in place, off the blob — an <iframe src> pointed at the API
    // path would not carry the bearer token.
    expect(within(preview).getByTitle("Preview of backend.pdf")).toHaveAttribute(
      "src",
      "blob:the-resume",
    );
  });

  it("previews the resume the button belongs to, on a page holding several", async () => {
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME, PLATFORM_RESUME]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Preview Platform Engineer" }),
    );

    await waitFor(() => expect(api.resumeFile).toHaveBeenCalledWith(2));
    expect(
      await screen.findByRole("dialog", { name: "Preview of platform.pdf" }),
    ).toBeInTheDocument();
  });

  it("closes the preview and releases the blob", async () => {
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Preview Staff Backend Engineer" }),
    );
    await user.click(await screen.findByRole("button", { name: "Close" }));

    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Preview of backend.pdf" }),
      ).not.toBeInTheDocument(),
    );
    // Held for as long as the preview is open, and no longer — a blob per click
    // that nobody revokes is a leak the tab keeps until it is closed.
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:the-resume");
  });

  it("reads a Word resume off the server's rendering of it", async () => {
    // A .docx is a zip of XML: pointed at one, a browser draws nothing, and
    // nothing is indistinguishable from a broken preview. So the server renders
    // it and the frame shows that — while Download still hands over the file.
    const user = userEvent.setup();
    URL.createObjectURL = vi
      .fn()
      .mockReturnValueOnce("blob:the-resume")
      .mockReturnValueOnce("blob:the-rendering");
    api.listResumes.mockResolvedValue([wordResume()]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Preview Staff Backend Engineer" }),
    );

    await waitFor(() => expect(api.resumePreview).toHaveBeenCalledWith(1));
    const preview = await screen.findByRole("dialog", {
      name: "Preview of backend.docx",
    });
    expect(within(preview).getByTitle("Preview of backend.docx")).toHaveAttribute(
      "src",
      "blob:the-rendering",
    );
    // The one thing that must not drift: what the user saves is the document,
    // not our rendering of it, because that is what the recruiter receives.
    expect(within(preview).getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      "blob:the-resume",
    );
  });

  it("says the frame holds a preview, not the document itself", async () => {
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([wordResume()]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Preview Staff Backend Engineer" }),
    );
    const preview = await screen.findByRole("dialog", {
      name: "Preview of backend.docx",
    });

    expect(
      within(preview).getByText(/recruiters receive the original file, unchanged/),
    ).toBeInTheDocument();
  });

  it("asks for no rendering of a PDF, which the browser draws itself", async () => {
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Preview Staff Backend Engineer" }),
    );

    await waitFor(() => expect(api.resumeFile).toHaveBeenCalledWith(1));
    expect(api.resumePreview).not.toHaveBeenCalled();
  });

  it("still opens the Word resume when there is no rendering to be had", async () => {
    // A 409 from the preview endpoint — a .docx the converter couldn't read, or
    // a format it doesn't convert. The document is still on file and still the
    // one that will be sent, so the overlay opens and offers it.
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([wordResume()]);
    api.resumePreview.mockRejectedValue(new Error("can't be shown in the browser"));
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Preview Staff Backend Engineer" }),
    );

    const preview = await screen.findByRole("dialog", {
      name: "Preview of backend.docx",
    });
    expect(
      within(preview).getByText(/can't be shown in the browser/),
    ).toBeInTheDocument();
    expect(
      within(preview).queryByTitle("Preview of backend.docx"),
    ).not.toBeInTheDocument();
    expect(
      within(preview).getByRole("link", { name: "Download to read it" }),
    ).toHaveAttribute("href", "blob:the-resume");
  });

  it("reports a failed open without touching the resume list", async () => {
    // The document not opening says nothing about the upload: it is still on
    // file, and it is still the one that will be sent.
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
    api.resumeFile.mockRejectedValue(new Error("We don't have the original file"));
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Preview Staff Backend Engineer" }),
    );

    expect(
      await screen.findByText("We don't have the original file"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: /Preview of/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Staff Backend Engineer")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Preview Staff Backend Engineer" }),
      ).toBeEnabled(),
    );
  });

  it("re-enables the row when a delete fails", async () => {
    // Without a finally the row's buttons stayed disabled and a reload was the
    // only way to try again — the same bug the drafts view had.
    const user = userEvent.setup();
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
    api.deleteResume.mockRejectedValue(new Error("Resume is in use"));
    renderSetup();

    await user.click(
      await screen.findByRole("button", { name: "Delete Staff Backend Engineer" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Delete resume" }),
    );

    expect((await screen.findAllByText("Resume is in use")).length).toBeGreaterThan(0);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Delete Staff Backend Engineer" }),
      ).toBeEnabled(),
    );
  });
});

describe("Setup — step 2: the search read off the resumes", () => {
  beforeEach(() => {
    api.onboarding.mockResolvedValue({
      ...ONBOARDING,
      resume_count: 2,
      next_step: "set_preferences",
    });
    api.listResumes.mockResolvedValue([BACKEND_RESUME, PLATFORM_RESUME]);
  });

  it("pre-fills from every resume, not just the default", async () => {
    renderSetup();

    await waitFor(() => expect(api.mergedSuggestedPreferences).toHaveBeenCalled());
    // Both resumes' titles reached the roles field — the merged read is the
    // point, and a per-resume read would only have produced the default's.
    await waitFor(() =>
      expect(tagsUnder(/Roles you want/).getByText("Staff Backend Engineer"))
        .toBeInTheDocument(),
    );
    expect(tagsUnder(/Roles you want/).getByText("Platform Engineer")).toBeInTheDocument();
    expect(tagsUnder(/^Where/).getByText("Austin, TX")).toBeInTheDocument();
  });

  it("shows what the parse read, so a bad extraction is visible", async () => {
    renderSetup();

    expect(await screen.findByText("Read from your 2 resumes")).toBeInTheDocument();
    expect(screen.getByText("lead · 10 yrs")).toBeInTheDocument();
    const skills = screen.getByText("Skills").parentElement;
    for (const skill of ["python", "kafka", "kubernetes"])
      expect(within(skills).getByText(skill)).toBeInTheDocument();
  });

  it("names the fields it filled in", async () => {
    renderSetup();

    const banner = await screen.findByText(/Filled in from your resumes/);
    expect(banner.parentElement.textContent).toContain("roles");
    expect(banner.parentElement.textContent).toContain("minimum salary");
  });

  it("carries the extracted salary expectation into the form", async () => {
    renderSetup();

    expect(await screen.findByLabelText(/Minimum salary/)).toHaveValue(175000);
  });

  it("saves the suggestions unchanged when the user just confirms", async () => {
    const user = userEvent.setup();
    renderSetup();

    await user.click(await screen.findByRole("button", { name: "Looks right — save" }));

    await waitFor(() => expect(api.updateAutopilot).toHaveBeenCalled());
    const saved = api.updateAutopilot.mock.calls[0][0];
    expect(saved.target_roles).toEqual([
      "Staff Backend Engineer",
      "Platform Engineer",
    ]);
    expect(saved.salary_min).toBe(175000);
    expect(saved.locations).toEqual(["San Francisco, CA", "Austin, TX"]);
    // The default resume drives tailoring unless the user picks another.
    expect(saved.resume_id).toBe(1);
  });

  it("saves the trial ramp, not just the auto-send toggle", async () => {
    // Dropping this one field is what made the trial inert: the wizard showed
    // "you approve the first few by hand", saved everything else, and the column
    // kept its default of 0 — so auto-send began on the very first email.
    const user = userEvent.setup();
    renderSetup();

    await user.click(await screen.findByRole("button", { name: "Looks right — save" }));

    await waitFor(() => expect(api.updateAutopilot).toHaveBeenCalled());
    expect(api.updateAutopilot.mock.calls[0][0].auto_send_trial_approvals).toBe(3);
  });

  it("an edit survives — it is not written back over by a re-suggest", async () => {
    const user = userEvent.setup();
    renderSetup();

    const salary = await screen.findByLabelText(/Minimum salary/);
    await user.clear(salary);
    await user.type(salary, "200000");

    await user.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(api.updateAutopilot).toHaveBeenCalled());
    expect(api.updateAutopilot.mock.calls[0][0].salary_min).toBe(200000);
  });

  it("never re-fills a form the user has already saved", async () => {
    api.getAutopilot.mockResolvedValue({
      ...PREFS,
      target_roles: ["Engineering Manager"],
      configured_at: "2026-07-01T00:00:00Z",
    });
    renderSetup();

    expect(await screen.findByText("Engineering Manager")).toBeInTheDocument();
    expect(api.mergedSuggestedPreferences).not.toHaveBeenCalled();
    expect(screen.queryByText(/Filled in from your resumes/)).not.toBeInTheDocument();
  });

  it("still renders an editable form when the suggestion read fails", async () => {
    api.mergedSuggestedPreferences.mockRejectedValue(new Error("offline"));
    renderSetup();

    expect(await screen.findByLabelText(/Roles you want/)).toBeInTheDocument();
    expect(screen.queryByText(/Filled in from your resumes/)).not.toBeInTheDocument();
  });

  it("reports a failed save", async () => {
    const user = userEvent.setup();
    api.updateAutopilot.mockRejectedValue(new Error("could not save"));
    renderSetup();

    await user.click(await screen.findByRole("button", { name: /save/i }));
    expect(await screen.findAllByText("could not save")).not.toHaveLength(0);
  });

  it("does not touch the preferences row before the step is reachable", async () => {
    api.onboarding.mockResolvedValue({ ...ONBOARDING, next_step: "upload_resume" });
    api.listResumes.mockResolvedValue([]);
    renderSetup();

    await screen.findByRole("heading", { name: "Add your resumes" });
    expect(api.getAutopilot).not.toHaveBeenCalled();
  });
});

describe("Setup — step 3: mailbox and switch", () => {
  const READY = {
    ...ONBOARDING,
    resume_count: 1,
    autopilot_configured: true,
    next_step: "connect_email",
  };

  beforeEach(() => {
    api.listResumes.mockResolvedValue([BACKEND_RESUME]);
  });

  it("asks for Gmail only after the search is confirmed", async () => {
    api.onboarding.mockResolvedValue({ ...ONBOARDING, next_step: "upload_resume" });
    api.listResumes.mockResolvedValue([]);
    renderSetup();

    await screen.findByRole("heading", { name: "Add your resumes" });
    expect(screen.queryByRole("button", { name: /Connect Gmail/ })).not.toBeInTheDocument();
  });

  it("opens the Google popup", async () => {
    const user = userEvent.setup();
    api.onboarding.mockResolvedValue(READY);
    api.gmailAuthorize.mockResolvedValue({ authorization_url: "https://accounts.google" });
    const open = vi.spyOn(window, "open").mockReturnValue({ closed: false });
    renderSetup();

    await user.click(await screen.findByRole("button", { name: /Connect Gmail/ }));

    await waitFor(() => expect(open).toHaveBeenCalled());
    open.mockRestore();
  });

  it("says so when Gmail is not configured on the server", async () => {
    api.onboarding.mockResolvedValue({ ...READY, gmail_configured: false });
    renderSetup();

    expect(
      await screen.findByText(/Gmail sign-in isn't configured on this server/),
    ).toBeInTheDocument();
  });

  it("offers the switch once a mailbox is connected", async () => {
    api.onboarding.mockResolvedValue({
      ...READY,
      gmail_connected: true,
      gmail_address: "candidate@gmail.com",
      next_step: "start_autopilot",
    });
    api.gmailStatus.mockResolvedValue({
      accounts: [{ id: 3, email: "candidate@gmail.com" }],
      push_configured: false,
    });
    renderSetup();

    expect(
      await screen.findByRole("button", { name: "Start autopilot" }),
    ).toBeInTheDocument();
  });

  it("starting sends the user to the pipeline", async () => {
    const user = userEvent.setup();
    api.onboarding.mockResolvedValue({
      ...READY,
      gmail_connected: true,
      gmail_address: "candidate@gmail.com",
      next_step: "start_autopilot",
    });
    renderSetup();

    await user.click(await screen.findByRole("button", { name: "Start autopilot" }));

    await waitFor(() =>
      expect(api.updateAutopilot).toHaveBeenCalledWith({ is_active: true }),
    );
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/pipeline"));
  });
});

describe("stepState", () => {
  it("marks a row done once the wizard is past its last step", () => {
    expect(stepState("set_preferences", ["upload_resume"])).toBe("done");
    expect(stepState("connect_email", ["set_preferences"])).toBe("done");
  });

  it("marks the row the wizard is currently on active", () => {
    expect(stepState("upload_resume", ["upload_resume"])).toBe("active");
    expect(stepState("connect_email", ["connect_email", "start_autopilot"])).toBe(
      "active",
    );
    expect(stepState("start_autopilot", ["connect_email", "start_autopilot"])).toBe(
      "active",
    );
  });

  it("marks rows the wizard has not reached todo", () => {
    expect(stepState("upload_resume", ["set_preferences"])).toBe("todo");
    expect(stepState("upload_resume", ["connect_email", "start_autopilot"])).toBe(
      "todo",
    );
  });

  it("marks everything done when the wizard is", () => {
    for (const names of [["upload_resume"], ["set_preferences"], ["connect_email"]])
      expect(stepState("done", names)).toBe("done");
  });

  it("treats an unrecognised step as nothing being reachable yet", () => {
    expect(stepState("who_knows", ["upload_resume"])).toBe("todo");
  });
});

describe("previewFilename", () => {
  it("is the upload's own name when the upload is on file", () => {
    expect(previewFilename({ filename: "backend.pdf", has_original_file: true })).toBe(
      "backend.pdf",
    );
  });

  it("is a .pdf when there is no upload, whatever the row is called", () => {
    // Rows from before the bytes were kept get a PDF rendered from the parse.
    // Offering that to save as `.docx` hands the user a file Word won't open.
    expect(
      previewFilename({ filename: "old-upload.docx", has_original_file: false }),
    ).toBe("old-upload.pdf");
  });

  it("names something even with no filename at all", () => {
    expect(previewFilename({ filename: null, has_original_file: false })).toBe(
      "resume.pdf",
    );
  });
});

describe("applySuggestions", () => {
  const suggestion = {
    suggestions: { target_roles: ["Staff Engineer"], min_fit_score: 60, salary_min: 150000 },
    sources: { target_roles: "resume", min_fit_score: "default", salary_min: "inferred" },
  };

  it("fills blank fields and reports what it badged", () => {
    const { values, filled } = applySuggestions(
      { target_roles: [], salary_min: null, min_fit_score: 70 },
      suggestion,
      { touched: new Set(), replaceable: [] },
    );
    expect(values.target_roles).toEqual(["Staff Engineer"]);
    expect(values.salary_min).toBe(150000);
    // Defaults ride along unbadged.
    expect(values.min_fit_score).toBe(60);
    expect(filled).toEqual(["target_roles", "salary_min"]);
  });

  it("never overwrites a field the user has touched", () => {
    const { values, filled } = applySuggestions(
      { target_roles: ["Mine"], salary_min: null },
      suggestion,
      { touched: new Set(["target_roles"]), replaceable: [] },
    );
    expect(values.target_roles).toEqual(["Mine"]);
    expect(filled).not.toContain("target_roles");
  });

  it("leaves an untouched non-blank value alone unless it put it there", () => {
    const previous = { target_roles: ["From an earlier suggestion"], salary_min: null };
    expect(
      applySuggestions(previous, suggestion, { touched: new Set(), replaceable: [] })
        .values.target_roles,
    ).toEqual(["From an earlier suggestion"]);
    // ...but a value this flow suggested is replaceable, so adding a resume
    // re-suggests rather than freezing the first guess.
    expect(
      applySuggestions(previous, suggestion, {
        touched: new Set(),
        replaceable: ["target_roles"],
      }).values.target_roles,
    ).toEqual(["Staff Engineer"]);
  });
});

describe("Setup — profiles: the several jobs you'd take", () => {
  const BACKEND_PROFILE = {
    id: 10,
    user_id: 1,
    resume_id: 1,
    name: "Backend Engineer",
    target_roles: ["Backend Engineer"],
    target_industries: [],
    skills: ["python"],
    location_preferences: ["Berlin"],
    remote_only: false,
    salary_min: 90000,
    salary_max: null,
    experience_level: "senior",
    is_active: true,
    is_default: true,
    resume_label: "Staff Backend Engineer",
    created_at: "2026-07-20T10:00:00Z",
  };
  const DEVOPS_PROFILE = {
    ...BACKEND_PROFILE,
    id: 11,
    resume_id: 2,
    name: "DevOps Engineer",
    target_roles: ["DevOps Engineer"],
    location_preferences: [],
    remote_only: true,
    is_default: false,
    resume_label: "Platform Engineer",
  };

  beforeEach(() => {
    api.onboarding.mockResolvedValue({
      ...ONBOARDING,
      resume_count: 2,
      next_step: "set_preferences",
    });
    api.listResumes.mockResolvedValue([BACKEND_RESUME, PLATFORM_RESUME]);
  });

  it("lists each profile with what it is actually chasing", async () => {
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE, DEVOPS_PROFILE]);
    renderSetup();

    expect(await screen.findByText("Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText("DevOps Engineer")).toBeInTheDocument();
    // The summary line is what makes the collapsed list readable at a glance.
    expect(screen.getByText(/Backend Engineer · Berlin · 90,000\+/)).toBeInTheDocument();
    expect(screen.getByText(/DevOps Engineer · remote only/)).toBeInTheDocument();
  });

  it("marks the default one", async () => {
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE, DEVOPS_PROFILE]);
    renderSetup();

    // Scoped to the rows, because the resume list above has a "default" of its
    // own and the two badges answer different questions.
    const backend = (await screen.findByText("Backend Engineer")).closest("li");
    const devops = screen.getByText("DevOps Engineer").closest("li");
    expect(within(backend).getByText("default")).toBeInTheDocument();
    expect(within(devops).queryByText("default")).not.toBeInTheDocument();
  });

  it("offers a one-click profile for a resume that hasn't got one", async () => {
    // The backend resume already has a profile; the platform resume doesn't.
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE]);
    const user = userEvent.setup();
    renderSetup();

    const button = await screen.findByRole("button", {
      name: /Profile from Platform Engineer/,
    });
    await user.click(button);
    expect(api.createProfileFromResume).toHaveBeenCalledWith({ resume_id: 2 });
  });

  it("switches a profile off without deleting it", async () => {
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE, DEVOPS_PROFILE]);
    const user = userEvent.setup();
    renderSetup();

    await screen.findByText("Backend Engineer");
    await user.click(screen.getAllByRole("button", { name: "Switch off" })[0]);
    expect(api.patchProfile).toHaveBeenCalledWith(10, { is_active: false });
  });

  it("edits the places a profile will work in", async () => {
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE]);
    const user = userEvent.setup();
    renderSetup();

    await user.click(await screen.findByRole("button", { name: /Edit Backend Engineer/ }));
    const field = screen.getByLabelText("Where you'd work");
    await user.type(field, "London");
    fireEvent.blur(field);

    await waitFor(() =>
      expect(api.patchProfile).toHaveBeenCalledWith(10, {
        location_preferences: ["Berlin", "London"],
      }),
    );
  });

  it("says the gate exists, next to the field that arms it", async () => {
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE]);
    const user = userEvent.setup();
    renderSetup();

    await user.click(await screen.findByRole("button", { name: /Edit Backend Engineer/ }));
    expect(
      screen.getByText(/won't auto-apply to roles outside these unless they're remote/),
    ).toBeInTheDocument();
  });

  it("confirms before deleting, since a profile is not a resume", async () => {
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE, DEVOPS_PROFILE]);
    const user = userEvent.setup();
    renderSetup();

    await screen.findByText("DevOps Engineer");
    await user.click(screen.getByRole("button", { name: "Delete DevOps Engineer" }));
    await user.click(await screen.findByRole("button", { name: "Delete profile" }));
    expect(api.deleteProfile).toHaveBeenCalledWith(11);
  });

  it("says which screen is in charge once there are two profiles", async () => {
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE, DEVOPS_PROFILE]);
    renderSetup();

    expect(
      await screen.findByText(/no longer what we search on/),
    ).toBeInTheDocument();
  });

  it("stays quiet about that with a single profile", async () => {
    api.listProfiles.mockResolvedValue([BACKEND_PROFILE]);
    renderSetup();

    await screen.findByText("Backend Engineer");
    expect(screen.queryByText(/no longer what we search on/)).not.toBeInTheDocument();
  });

  it("renders the rest of the page when the profile list fails to load", async () => {
    // Profiles are one section; losing them must not cost the user their
    // mailbox connection or their uploads.
    api.listProfiles.mockRejectedValue(new Error("boom"));
    renderSetup();

    expect(
      await screen.findByRole("heading", { name: "Confirm your search" }),
    ).toBeInTheDocument();
  });
});
