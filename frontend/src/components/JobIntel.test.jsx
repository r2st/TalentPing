import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import JobIntel, { AlsoOn, CompanyCard, SalaryCard, ScoutTake } from "./JobIntel";

vi.mock("../lib/api", () => ({ api: { jobIntel: vi.fn() } }));

import { api } from "../lib/api";

const BAND = {
  role_family: "backend_engineer",
  role_label: "Backend Engineer",
  seniority: "senior",
  location_key: "us_sf",
  location_label: "San Francisco Bay Area",
  currency: "USD",
  min: 194000,
  median: 237000,
  max: 303000,
  source: "modelled",
  sample_size: 0,
};

const INTEL = {
  job_id: 1,
  llm_fit_score: 88,
  llm_reasoning: "The settlement work maps onto their payments platform.",
  salary: {
    band: BAND,
    comparison: {
      offered_min: 160000,
      offered_max: 200000,
      offered_mid: 180000,
      delta: -0.24,
      verdict: "below",
      label: "About 24% below the market median for this role.",
    },
    is_estimate: true,
  },
  company: {
    name: "Northwind Labs",
    industry: "fintech",
    size: "201-500",
    founded_year: 2017,
    headquarters: "San Francisco, CA",
    funding_stage: "series_b",
    funding_total: "$45M",
    tech_stack: ["python", "fastapi"],
    news: [{ title: "Raised a Series B", published: "2026-05", source: "TechCrunch" }],
    summary: "Payments infrastructure.",
    source: "llm",
    status: "ok",
  },
  also_on: [
    { source: "serpapi", url: "https://a.example" },
    { source: "remoteok", url: "https://b.example" },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.jobIntel.mockResolvedValue(INTEL);
});

describe("ScoutTake", () => {
  it("shows the score and the reasoning behind it", () => {
    render(<ScoutTake score={88} reasoning="Strong trajectory match." />);
    expect(screen.getByText("88")).toBeInTheDocument();
    expect(screen.getByText("Strong trajectory match.")).toBeInTheDocument();
  });

  it("says the deterministic score is untouched", () => {
    render(<ScoutTake score={88} reasoning="x" />);
    expect(screen.getByText(/never changes/)).toBeInTheDocument();
  });

  it("renders nothing when Scout hasn't looked at this job", () => {
    const { container } = render(<ScoutTake score={null} reasoning={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("SalaryCard", () => {
  it("shows the band and where this posting sits in it", () => {
    render(<SalaryCard salary={INTEL.salary} />);
    expect(screen.getByText("$237,000")).toBeInTheDocument();
    expect(screen.getByText("below market")).toBeInTheDocument();
    expect(screen.getByText(/24% below the market median/)).toBeInTheDocument();
  });

  it("labels a modelled band as an estimate", () => {
    render(<SalaryCard salary={INTEL.salary} />);
    // The candidate may negotiate against this number; they should know we
    // modelled it rather than observed it.
    expect(screen.getByText(/modelled estimate, not observed pay data/)).toBeInTheDocument();
  });

  it("says a posting publishes nothing rather than implying it's average", () => {
    render(
      <SalaryCard
        salary={{
          band: BAND,
          comparison: {
            offered_min: null,
            offered_max: null,
            offered_mid: null,
            delta: null,
            verdict: "unknown",
            label: "This posting doesn't publish a salary.",
          },
          is_estimate: true,
        }}
      />,
    );
    expect(screen.getByText("not published")).toBeInTheDocument();
    expect(screen.getByText(/doesn't publish a salary/)).toBeInTheDocument();
  });

  it("renders nothing without a band", () => {
    const { container } = render(<SalaryCard salary={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("CompanyCard", () => {
  it("shows the facts it has", () => {
    render(<CompanyCard company={INTEL.company} />);
    expect(screen.getByText("fintech")).toBeInTheDocument();
    expect(screen.getByText("201-500 people")).toBeInTheDocument();
    expect(screen.getByText("Series B · $45M")).toBeInTheDocument();
    expect(screen.getByText(/Raised a Series B/)).toBeInTheDocument();
  });

  it("omits what it doesn't know instead of printing 'unknown'", () => {
    render(<CompanyCard company={{ name: "Acme", industry: "fintech", source: "heuristic" }} />);
    expect(screen.queryByText(/unknown/i)).not.toBeInTheDocument();
    expect(screen.queryByText("HQ")).not.toBeInTheDocument();
  });

  it("flags a recalled card as worth confirming", () => {
    render(<CompanyCard company={INTEL.company} />);
    expect(screen.getByText(/Recalled by Scout rather than looked up/)).toBeInTheDocument();
  });

  it("does not flag a card read off the posting", () => {
    render(<CompanyCard company={{ ...INTEL.company, source: "heuristic" }} />);
    expect(screen.queryByText(/Recalled by Scout/)).not.toBeInTheDocument();
  });

  it("says so when nothing was found", () => {
    render(<CompanyCard company={{ name: "Nobody Ltd", source: "heuristic", status: "empty" }} />);
    expect(screen.getByText(/Nothing reliable found/)).toBeInTheDocument();
  });
});

describe("AlsoOn", () => {
  it("links every board carrying the role", () => {
    render(<AlsoOn links={INTEL.also_on} />);
    expect(screen.getByRole("link", { name: "serpapi" })).toHaveAttribute(
      "href",
      "https://a.example",
    );
    expect(screen.getByRole("link", { name: "remoteok" })).toBeInTheDocument();
  });

  it("renders nothing for a role seen on one board", () => {
    const { container } = render(<AlsoOn links={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("JobIntel panel", () => {
  it("loads on mount and renders all three halves", async () => {
    render(<JobIntel jobId={1} />);

    expect(await screen.findByText(/settlement work maps/)).toBeInTheDocument();
    expect(screen.getByText("below market")).toBeInTheDocument();
    expect(screen.getByText("Northwind Labs")).toBeInTheDocument();
    expect(api.jobIntel).toHaveBeenCalledWith(1);
  });

  it("surfaces a failure instead of rendering an empty panel", async () => {
    api.jobIntel.mockRejectedValue(new Error("intel unavailable"));
    render(<JobIntel jobId={1} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("intel unavailable");
  });

  it("re-researches the company on demand", async () => {
    const user = userEvent.setup();
    render(<JobIntel jobId={1} />);

    await user.click(await screen.findByRole("button", { name: "Refresh" }));
    await waitFor(() =>
      expect(api.jobIntel).toHaveBeenCalledWith(1, { refresh: true }),
    );
  });
});
