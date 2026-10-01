import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../components/ui/Toast";
import Pipeline from "./Pipeline";

vi.mock("../lib/api", () => ({
  api: {
    dashboard: vi.fn(),
    dashboardQueue: vi.fn(),
    listCampaigns: vi.fn(),
    getAutopilot: vi.fn(),
    updateAutopilot: vi.fn(),
    runAutopilot: vi.fn(),
    autopilotReputation: vi.fn(),
    onboarding: vi.fn(),
    analytics: vi.fn(),
    subjectVariants: vi.fn(),
    exportApplicationsCsv: vi.fn(),
    pauseCampaign: vi.fn(),
    resumeCampaign: vi.fn(),
  },
}));

import { api } from "../lib/api";

const DASHBOARD = {
  stats: {
    total_applications: 4,
    active: 2,
    responded: 2,
    interviews: 1,
    response_rate: 0.5,
    interview_rate: 0.25,
    average_fit_score: 78,
  },
  pipeline: [
    { key: "applied", label: "Applied", count: 4, rate: 1 },
    { key: "responded", label: "Responded", count: 2, rate: 0.5 },
    { key: "interview", label: "Interview", count: 1, rate: 0.25 },
  ],
  applications: [
    {
      application_id: 1,
      company: "Acme",
      role: "Senior Backend Engineer",
      status: "INTERESTED",
      contact: "dana@acme.example",
      next_follow_up_at: null,
      last_activity_at: "2026-07-20T10:00:00Z",
    },
  ],
  activity: [
    {
      kind: "reply_received",
      at: "2026-07-20T10:00:00Z",
      company: "Acme",
      summary: "Sounds interesting.",
    },
  ],
  filters: { companies: ["Acme"], campaigns: [], statuses: ["INTERESTED"] },
};

const ANALYTICS = {
  total_applications: 12,
  responses: 4,
  interviews: 2,
  response_rate: 0.333,
  interview_rate: 0.167,
  median_days_to_reply: 2.5,
  bucket: "week",
  trend: [
    { period: "2026-07-06", label: "06 Jul", applications: 5, responses: 1, response_rate: 0.2 },
    { period: "2026-07-13", label: "13 Jul", applications: 7, responses: 3, response_rate: 0.43 },
  ],
  by_status: [
    { status: "OUTREACH_SENT", label: "Sent", count: 8, share: 0.667 },
    { status: "REPLIED", label: "Replied", count: 4, share: 0.333 },
  ],
  resumes: [
    {
      resume_id: 1,
      label: "jordan-backend.pdf",
      applications: 8,
      responses: 4,
      interviews: 2,
      response_rate: 0.5,
      is_best: true,
    },
    {
      resume_id: 2,
      label: "jordan-generic.pdf",
      applications: 4,
      responses: 0,
      interviews: 0,
      response_rate: 0,
      is_best: false,
    },
  ],
  companies: [
    { name: "Acme", applications: 6, responses: 3, interviews: 1, response_rate: 0.5 },
  ],
  industries: [
    { name: "fintech", applications: 8, responses: 4, interviews: 2, response_rate: 0.5 },
    { name: "gaming", applications: 1, responses: 1, interviews: 0, response_rate: 1 },
  ],
  min_sample: 3,
};

const AUTOPILOT = {
  is_active: false,
  min_fit_score: 70,
  daily_application_limit: 10,
  applications_created: 6,
  last_run_at: "2026-07-20T09:00:00Z",
  last_error: null,
};

function renderPipeline() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <Pipeline />
      </ToastProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.dashboard.mockResolvedValue(DASHBOARD);
  api.dashboardQueue.mockResolvedValue({ pending_sends: 2, follow_ups: [] });
  api.listCampaigns.mockResolvedValue([]);
  api.getAutopilot.mockResolvedValue(AUTOPILOT);
  api.updateAutopilot.mockImplementation((changes) =>
    Promise.resolve({ ...AUTOPILOT, ...changes }),
  );
  api.runAutopilot.mockResolvedValue({ applied: 3, scanned: 9, budget: 10 });
  api.autopilotReputation.mockResolvedValue([]);
  api.onboarding.mockResolvedValue({
    complete: true,
    gmail_connected: true,
    resume_count: 1,
  });
  api.analytics.mockResolvedValue(ANALYTICS);
  api.subjectVariants.mockResolvedValue([]);
  api.exportApplicationsCsv.mockResolvedValue("application_id,company\n1,Acme\n");
});

describe("Pipeline", () => {
  it("shows the funnel and the applications behind it", async () => {
    renderPipeline();

    expect(await screen.findByText("Where every application stands")).toBeInTheDocument();
    expect(screen.getByText("Senior Backend Engineer")).toBeInTheDocument();
    expect(screen.getByText("Responded")).toBeInTheDocument();
  });
});

/* -------------------------------------------------------------------------- */
/* Autopilot, folded into the pipeline header                                 */
/* -------------------------------------------------------------------------- */

describe("Pipeline autopilot bar", () => {
  it("puts the master switch on the pipeline, not a page of its own", async () => {
    renderPipeline();
    expect(await screen.findByRole("button", { name: /autopilot off/i })).toBeInTheDocument();
  });

  it("toggles autopilot from the header", async () => {
    const user = userEvent.setup();
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: /autopilot off/i }));

    await waitFor(() => expect(api.updateAutopilot).toHaveBeenCalledWith({ is_active: true }));
    expect(await screen.findByRole("button", { name: /autopilot on/i })).toBeInTheDocument();
  });

  it("runs autopilot on demand and reports what it did", async () => {
    const user = userEvent.setup();
    api.getAutopilot.mockResolvedValue({ ...AUTOPILOT, is_active: true });
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: "Run now" }));

    await waitFor(() => expect(api.runAutopilot).toHaveBeenCalled());
    expect(await screen.findByText(/Applied to 3 of 9 matches/)).toBeInTheDocument();
  });

  it("will not run while autopilot is switched off", async () => {
    renderPipeline();
    expect(await screen.findByRole("button", { name: "Run now" })).toBeDisabled();
  });

  it("will not run before Gmail is connected", async () => {
    api.getAutopilot.mockResolvedValue({ ...AUTOPILOT, is_active: true });
    api.onboarding.mockResolvedValue({
      complete: true,
      gmail_connected: false,
      resume_count: 1,
    });
    renderPipeline();

    expect(await screen.findByRole("button", { name: "Run now" })).toBeDisabled();
  });

  it("keeps the rest of the page alive when autopilot cannot be read", async () => {
    api.getAutopilot.mockRejectedValue(new Error("autopilot unavailable"));
    renderPipeline();

    expect(await screen.findByText("Where every application stands")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /autopilot/i })).not.toBeInTheDocument();
  });

  it("restores the real value when a toggle fails", async () => {
    const user = userEvent.setup();
    api.updateAutopilot.mockRejectedValue(new Error("save failed"));
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: /autopilot off/i }));

    // Optimistic flip, then reconciled back from the server.
    expect(await screen.findByRole("button", { name: /autopilot off/i })).toBeInTheDocument();
    // Reported in both the banner and a toast.
    expect((await screen.findAllByText("save failed")).length).toBeGreaterThan(0);
  });
});

/* -------------------------------------------------------------------------- */
/* Analytics                                                                  */
/* -------------------------------------------------------------------------- */

describe("Pipeline analytics", () => {
  it("does not read analytics until the panel is opened", async () => {
    renderPipeline();
    await screen.findByText("Where every application stands");

    expect(api.analytics).not.toHaveBeenCalled();
  });

  it("shows the headline rates once opened", async () => {
    const user = userEvent.setup();
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: /Analytics/ }));

    await waitFor(() => expect(api.analytics).toHaveBeenCalled());
    expect(await screen.findByText("Median days to reply")).toBeInTheDocument();
    expect(screen.getByText("2.5")).toBeInTheDocument();
  });

  it("charts the response rate over time", async () => {
    const user = userEvent.setup();
    renderPipeline();
    await user.click(await screen.findByRole("button", { name: /Analytics/ }));

    expect(await screen.findByText("Response rate over time")).toBeInTheDocument();
    expect(screen.getByText("13 Jul")).toBeInTheDocument();
    // Volume is shown next to the rate: a 100% week of one send is not a record.
    expect(screen.getByText("n=7")).toBeInTheDocument();
  });

  it("re-reads when the bucket changes", async () => {
    const user = userEvent.setup();
    renderPipeline();
    await user.click(await screen.findByRole("button", { name: /Analytics/ }));
    await waitFor(() => expect(api.analytics).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "By month" }));

    await waitFor(() =>
      expect(api.analytics).toHaveBeenLastCalledWith(
        expect.objectContaining({ bucket: "month" }),
      ),
    );
  });

  it("marks the best-performing resume", async () => {
    const user = userEvent.setup();
    renderPipeline();
    await user.click(await screen.findByRole("button", { name: /Analytics/ }));

    const best = (await screen.findByText("jordan-backend.pdf")).closest("li");
    expect(within(best).getByText("best")).toBeInTheDocument();
    const worse = screen.getByText("jordan-generic.pdf").closest("li");
    expect(within(worse).queryByText("best")).not.toBeInTheDocument();
  });

  it("flags a segment too thin to draw a conclusion from", async () => {
    const user = userEvent.setup();
    renderPipeline();
    await user.click(await screen.findByRole("button", { name: /Analytics/ }));

    // 1 application at 100% is flagged rather than hidden, so the user sees the
    // row exists without reading its percentage as a finding.
    const thin = (await screen.findByText("gaming")).closest("li");
    expect(within(thin).getByText("·thin")).toBeInTheDocument();
    const solid = screen.getByText("fintech").closest("li");
    expect(within(solid).queryByText("·thin")).not.toBeInTheDocument();
  });

  it("lists the most responsive industries", async () => {
    const user = userEvent.setup();
    renderPipeline();
    await user.click(await screen.findByRole("button", { name: /Analytics/ }));

    expect(await screen.findByText("Most responsive industries")).toBeInTheDocument();
    expect(screen.getByText("Most responsive companies")).toBeInTheDocument();
  });

  it("says so when there is nothing to analyse yet", async () => {
    const user = userEvent.setup();
    api.analytics.mockResolvedValue({ ...ANALYTICS, total_applications: 0 });
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: /Analytics/ }));

    expect(await screen.findByText(/Nothing to analyse yet/)).toBeInTheDocument();
  });

  it("surfaces an analytics failure without taking the pipeline down", async () => {
    const user = userEvent.setup();
    api.analytics.mockRejectedValue(new Error("analytics unavailable"));
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: /Analytics/ }));

    expect(await screen.findByText("analytics unavailable")).toBeInTheDocument();
    expect(screen.getByText("Senior Backend Engineer")).toBeInTheDocument();
  });
});

/* -------------------------------------------------------------------------- */
/* Engagement + subject experiments                                           */
/* -------------------------------------------------------------------------- */

const ENGAGEMENT = {
  tracked: 20,
  opened: 12,
  clicked: 3,
  open_rate: 0.6,
  click_rate: 0.15,
  click_to_open_rate: 0.25,
  reliable: true,
};

async function openAnalytics() {
  const user = userEvent.setup();
  renderPipeline();
  await user.click(await screen.findByRole("button", { name: /Analytics/ }));
  return user;
}

describe("Pipeline engagement stats", () => {
  it("shows open and click rates", async () => {
    api.analytics.mockResolvedValue({ ...ANALYTICS, engagement: ENGAGEMENT });
    await openAnalytics();

    expect(await screen.findByText("Engagement")).toBeInTheDocument();
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getByText("Clicked after opening")).toBeInTheDocument();
  });

  it("always captions the numbers as approximate", async () => {
    // Not softenable: proxies prefetch the pixel and gateways block images, so
    // presenting these as a measurement would be a lie the user acts on.
    api.analytics.mockResolvedValue({ ...ANALYTICS, engagement: ENGAGEMENT });
    await openAnalytics();

    expect(await screen.findByText(/approximate/)).toBeInTheDocument();
  });

  it("warns when the sample is too thin to read", async () => {
    api.analytics.mockResolvedValue({
      ...ANALYTICS,
      engagement: { ...ENGAGEMENT, tracked: 2, reliable: false },
    });
    await openAnalytics();

    expect(
      await screen.findByText(/Too few tracked sends/),
    ).toBeInTheDocument();
  });

  it("is hidden entirely when nothing has been tracked", async () => {
    api.analytics.mockResolvedValue({
      ...ANALYTICS,
      engagement: { ...ENGAGEMENT, tracked: 0 },
    });
    await openAnalytics();

    await screen.findByText("Response rate over time");
    expect(screen.queryByText("Engagement")).not.toBeInTheDocument();
  });
});

describe("Pipeline subject experiments", () => {
  const EXPERIMENT = {
    campaign_id: 1,
    campaign_name: "Fintech backend",
    confident: true,
    variants: [
      {
        id: 1,
        label: "A",
        text: "Jordan Candidate — Backend Engineer",
        sends: 12,
        opens: 8,
        replies: 2,
        open_rate: 0.667,
        reply_rate: 0.167,
        is_winner: true,
        is_active: true,
        generated_with: "llm",
      },
      {
        id: 2,
        label: "B",
        text: "Backend Engineer at Acme?",
        sends: 11,
        opens: 3,
        replies: 0,
        open_rate: 0.273,
        reply_rate: 0,
        is_winner: false,
        is_active: false,
        generated_with: "llm",
      },
    ],
  };

  it("lists each variant with its open rate and marks the winner", async () => {
    api.subjectVariants.mockResolvedValue([EXPERIMENT]);
    await openAnalytics();

    expect(await screen.findByText("Subject lines")).toBeInTheDocument();
    const winner = screen
      .getByText("Jordan Candidate — Backend Engineer")
      .closest("li");
    expect(within(winner).getByText("★")).toBeInTheDocument();
    expect(within(winner).getByText("67%")).toBeInTheDocument();

    const loser = screen.getByText("Backend Engineer at Acme?").closest("li");
    expect(within(loser).queryByText("★")).not.toBeInTheDocument();
  });

  it("captions a thin experiment as early rather than hiding it", async () => {
    api.subjectVariants.mockResolvedValue([{ ...EXPERIMENT, confident: false }]);
    await openAnalytics();

    expect(await screen.findByText(/not enough data yet/)).toBeInTheDocument();
  });

  it("renders nothing when no campaign is running an experiment", async () => {
    api.subjectVariants.mockResolvedValue([]);
    await openAnalytics();

    await screen.findByText("Response rate over time");
    expect(screen.queryByText("Subject lines")).not.toBeInTheDocument();
  });

  it("surfaces a failure without taking the panel down", async () => {
    api.subjectVariants.mockRejectedValue(new Error("variants unavailable"));
    await openAnalytics();

    expect(await screen.findByText("variants unavailable")).toBeInTheDocument();
    expect(screen.getByText("Response rate over time")).toBeInTheDocument();
  });
});

/* -------------------------------------------------------------------------- */
/* CSV export                                                                 */
/* -------------------------------------------------------------------------- */

describe("Pipeline CSV export", () => {
  beforeEach(() => {
    // jsdom implements neither of these.
    URL.createObjectURL = vi.fn(() => "blob:mock");
    URL.revokeObjectURL = vi.fn();
  });

  it("downloads the table as a dated file", async () => {
    const user = userEvent.setup();
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: "Export CSV" }));

    await waitFor(() => expect(api.exportApplicationsCsv).toHaveBeenCalled());
    expect(click).toHaveBeenCalled();
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(await screen.findByText("Downloaded.")).toBeInTheDocument();
    click.mockRestore();
  });

  it("exports the same slice the table is showing", async () => {
    const user = userEvent.setup();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    renderPipeline();

    await user.selectOptions(await screen.findByLabelText("Company"), "Acme");
    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    await waitFor(() =>
      expect(api.exportApplicationsCsv).toHaveBeenCalledWith(
        expect.objectContaining({ company: "Acme" }),
      ),
    );
  });

  it("releases the object URL it created", async () => {
    const user = userEvent.setup();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: "Export CSV" }));

    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock"));
  });

  it("reports a failed export instead of a silent no-op", async () => {
    const user = userEvent.setup();
    api.exportApplicationsCsv.mockRejectedValue(new Error("export failed"));
    renderPipeline();

    await user.click(await screen.findByRole("button", { name: "Export CSV" }));

    expect((await screen.findAllByText("export failed")).length).toBeGreaterThan(0);
  });
});
