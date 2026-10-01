import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConfirmProvider } from "../components/ui/ConfirmDialog";
import { ToastProvider } from "../components/ui/Toast";
import Jobs from "./Jobs";

vi.mock("../lib/api", () => ({
  api: {
    listJobs: vi.fn(),
    listSearches: vi.fn(),
    jobProviders: vi.fn(),
    listProfiles: vi.fn(),
    patchJob: vi.fn(),
    runSearch: vi.fn(),
    createSearch: vi.fn(),
    patchSearch: vi.fn(),
    deleteSearch: vi.fn(),
    bulkJobs: vi.fn(),
    tailor: vi.fn(),
    downloadTailored: vi.fn(),
  },
}));

import { api } from "../lib/api";

const JOB = {
  id: 1,
  title: "Senior Backend Engineer",
  company: "Acme",
  location: "San Francisco",
  remote: false,
  salary_text: "$160k-$200k",
  source: "remoteok",
  url: "https://acme.example/jobs/1",
  status: "NEW",
  fit_score: 82,
  created_at: "2026-07-20T10:00:00Z",
};

const SECOND_JOB = {
  ...JOB,
  id: 2,
  title: "Staff Engineer",
  company: "Northwind",
  fit_score: 71,
};

const TAILORED = {
  tailored: {
    id: 9,
    tailored_summary: "Backend engineer with payments depth.",
    ordered_skills: ["python", "fastapi", "aws"],
    matched_keywords: ["python"],
    missing_keywords: ["rust"],
    generated_with: "llm",
    cover_letter: "Dear Acme,",
  },
  parsed_job: { title: "Senior Backend Engineer", company: "Acme" },
  fit: {
    overall: 82,
    summary: "Strong match — worth a tailored application today.",
    breakdown: [],
  },
};

function renderJobs() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ConfirmProvider>
          <Jobs />
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listJobs.mockResolvedValue([JOB, SECOND_JOB]);
  api.listSearches.mockResolvedValue([]);
  api.jobProviders.mockResolvedValue({
    google_jobs_serpapi: true,
    public_boards: [],
    form_autofill: false,
  });
  api.listProfiles.mockResolvedValue([]);
  api.patchJob.mockResolvedValue({});
  api.bulkJobs.mockResolvedValue({
    action: "dismiss",
    updated: 2,
    deleted: 0,
    skipped: 0,
    not_found: [],
  });
  api.tailor.mockResolvedValue(TAILORED);
  api.downloadTailored.mockResolvedValue("# Tailored resume");
});

describe("Jobs feed", () => {
  it("lists what the monitor found", async () => {
    renderJobs();
    expect(await screen.findByText("Senior Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText(/Acme · San Francisco/)).toBeInTheDocument();
  });
});

describe("Jobs bulk actions", () => {
  it("acts on every selected job at once", async () => {
    const user = userEvent.setup();
    renderJobs();

    await user.click(await screen.findByLabelText("Select Senior Backend Engineer"));
    await user.click(screen.getByLabelText("Select Staff Engineer"));
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(api.bulkJobs).toHaveBeenCalledWith([1, 2], "dismiss"));
    expect(await screen.findByText(/2 jobs dismissed/)).toBeInTheDocument();
  });

  it("selects and clears everything from the header checkbox", async () => {
    const user = userEvent.setup();
    renderJobs();

    await user.click(await screen.findByLabelText("Select all jobs"));
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Clear selection"));
    expect(screen.queryByText("2 selected")).not.toBeInTheDocument();
  });

  it("offers no bulk actions until something is selected", async () => {
    renderJobs();
    await screen.findByText("Senior Backend Engineer");

    expect(screen.queryByRole("button", { name: "Mark applied" })).not.toBeInTheDocument();
    // But the control itself is visible, so the feature is discoverable.
    expect(screen.getByLabelText("Select all jobs")).toBeInTheDocument();
  });

  it("confirms before archiving, because archiving deletes", async () => {
    const user = userEvent.setup();
    renderJobs();

    await user.click(await screen.findByLabelText("Select Senior Backend Engineer"));
    await user.click(screen.getByRole("button", { name: "Archive" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/removed from your feed for good/)).toBeInTheDocument();
    expect(api.bulkJobs).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(api.bulkJobs).toHaveBeenCalledWith([1], "archive"));
  });

  it("does not archive when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    renderJobs();

    await user.click(await screen.findByLabelText("Select Senior Backend Engineer"));
    await user.click(screen.getByRole("button", { name: "Archive" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.bulkJobs).not.toHaveBeenCalled();
  });

  it("reports a bulk failure instead of silently doing nothing", async () => {
    const user = userEvent.setup();
    api.bulkJobs.mockRejectedValue(new Error("Feed is unavailable"));
    renderJobs();

    await user.click(await screen.findByLabelText("Select Staff Engineer"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findAllByText("Feed is unavailable")).length).toBeGreaterThan(0);
    // The selection survives, so the user can retry without re-ticking.
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("drops selected jobs that have left the feed", async () => {
    const user = userEvent.setup();
    renderJobs();

    await user.click(await screen.findByLabelText("Select all jobs"));
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    // A dismissal removes one from the feed; its id must not be sent again.
    api.listJobs.mockResolvedValue([JOB]);
    await user.click(screen.getByRole("button", { name: "Mark applied" }));
    await waitFor(() => expect(api.bulkJobs).toHaveBeenCalledWith([1, 2], "apply"));

    await waitFor(() =>
      expect(screen.queryByText("Staff Engineer")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
  });
});

describe("Jobs tailor drawer", () => {
  it("tailors against the job you picked, without leaving the feed", async () => {
    const user = userEvent.setup();
    renderJobs();

    await user.click((await screen.findAllByRole("button", { name: "Tailor" }))[0]);

    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByText("Senior Backend Engineer")).toBeInTheDocument();

    await user.click(within(drawer).getByRole("button", { name: "Tailor my resume" }));

    // The letter is asked for in the same round trip: the drawer shows both,
    // and re-parsing the JD twice to get them separately is wasteful.
    await waitFor(() =>
      expect(api.tailor).toHaveBeenCalledWith({
        job_posting_id: 1,
        include_cover_letter: true,
      }),
    );
    expect(
      await screen.findByText("Backend engineer with payments depth."),
    ).toBeInTheDocument();
    // The honest half: what the posting asked for and the resume doesn't have.
    expect(screen.getByText("rust")).toBeInTheDocument();
  });

  it("marks a freshly tailored job as tailored", async () => {
    const user = userEvent.setup();
    renderJobs();

    await user.click((await screen.findAllByRole("button", { name: "Tailor" }))[0]);
    const drawer = await screen.findByRole("dialog");
    await user.click(within(drawer).getByRole("button", { name: "Tailor my resume" }));

    await waitFor(() =>
      expect(api.patchJob).toHaveBeenCalledWith(1, { status: "TAILORED" }),
    );
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    renderJobs();

    await user.click((await screen.findAllByRole("button", { name: "Tailor" }))[0]);
    await screen.findByRole("dialog");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("surfaces a tailoring failure in the drawer", async () => {
    const user = userEvent.setup();
    api.tailor.mockRejectedValue(new Error("Upload a resume first"));
    renderJobs();

    await user.click((await screen.findAllByRole("button", { name: "Tailor" }))[0]);
    const drawer = await screen.findByRole("dialog");
    await user.click(within(drawer).getByRole("button", { name: "Tailor my resume" }));

    expect((await screen.findAllByText("Upload a resume first")).length).toBeGreaterThan(0);
  });
});

describe("Jobs keyboard shortcuts", () => {
  it("moves the cursor with j and k, and opens with Enter", async () => {
    const user = userEvent.setup();
    renderJobs();
    await screen.findByText("Senior Backend Engineer");

    await user.keyboard("jj");
    await user.keyboard("{Enter}");

    const drawer = await screen.findByRole("dialog");
    expect(within(drawer).getByText("Staff Engineer")).toBeInTheDocument();
  });

  it("selects the focused job with x", async () => {
    const user = userEvent.setup();
    renderJobs();
    await screen.findByText("Senior Backend Engineer");

    await user.keyboard("jx");
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("unwinds one layer at a time on Escape", async () => {
    const user = userEvent.setup();
    renderJobs();
    await screen.findByText("Senior Backend Engineer");

    await user.keyboard("jx");
    await user.keyboard("{Enter}");
    await screen.findByRole("dialog");

    // First Escape closes the drawer, and must not also drop the selection.
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByText("1 selected")).not.toBeInTheDocument();
  });

  it("stays out of the way while the user types in a filter", async () => {
    const user = userEvent.setup();
    renderJobs();

    const company = await screen.findByLabelText("Company");
    await user.type(company, "jk");

    expect(company).toHaveValue("jk");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("Jobs feed — which profile matched", () => {
  const MATCHED = {
    ...JOB,
    id: 7,
    title: "DevOps Engineer",
    matched_profile_id: 11,
    matched_profile_name: "DevOps Engineer profile",
  };

  it("names the profile that won the job when there is more than one", async () => {
    api.listJobs.mockResolvedValue([MATCHED]);
    api.listProfiles.mockResolvedValue([{ id: 10 }, { id: 11 }]);
    renderJobs();

    expect(await screen.findByText("DevOps Engineer profile")).toBeInTheDocument();
  });

  it("stays quiet with a single profile, where the answer says nothing", async () => {
    api.listJobs.mockResolvedValue([MATCHED]);
    api.listProfiles.mockResolvedValue([{ id: 10 }]);
    renderJobs();

    await screen.findByText("DevOps Engineer");
    expect(screen.queryByText("DevOps Engineer profile")).not.toBeInTheDocument();
  });

  it("shows nothing for a job no profile matched", async () => {
    api.listJobs.mockResolvedValue([{ ...MATCHED, matched_profile_name: null }]);
    api.listProfiles.mockResolvedValue([{ id: 10 }, { id: 11 }]);
    renderJobs();

    await screen.findByText("DevOps Engineer");
    expect(screen.queryByText("DevOps Engineer profile")).not.toBeInTheDocument();
  });
});
