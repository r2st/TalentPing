import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RecruiterStats, { TrendBars } from "./RecruiterStats";

vi.mock("../lib/api", () => ({ api: { recruiterStats: vi.fn() } }));

import { api } from "../lib/api";

const PAYLOAD = {
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
    escalated: 0,
    dismissed: 5,
    failed: 0,
    deferred: 0,
  },
  skipped: [{ reason: "skipped_known", label: "Already checked", count: 8602 }],
  trend: [
    { period: "2026-07-21", label: "21 Jul", scanned: 1240, detected: 11 },
    { period: "2026-07-22", label: "22 Jul", scanned: 980, detected: 0 },
    { period: "2026-07-23", label: "23 Jul", scanned: 900, detected: 4 },
  ],
  push: {
    configured: true,
    healthy: true,
    covering: true,
    notifications: 318,
    scans_from_push: 300,
    scans_from_beat: 100,
    scans_manual: 12,
  },
};

const EMPTY = {
  days: 30,
  bucket: "day",
  totals: { scans: 0, listed: 0, detected: 0 },
  skipped: [],
  trend: [],
  push: { configured: false },
};

beforeEach(() => {
  vi.clearAllMocks();
  api.recruiterStats.mockResolvedValue(PAYLOAD);
});

describe("RecruiterStats", () => {
  it("asks for thirty days of daily buckets by default", async () => {
    render(<RecruiterStats />);

    await waitFor(() =>
      expect(api.recruiterStats).toHaveBeenCalledWith({ days: 30, bucket: "day" }),
    );
  });

  it("shows the scanned total, not just what was opened", async () => {
    render(<RecruiterStats />);

    // 9,120 listed against 388 examined — the gap is the point of the filters,
    // and showing only the small number would flatter the product.
    expect(await screen.findByText("9,120")).toBeInTheDocument();
    expect(screen.getByText("Scanned")).toBeInTheDocument();
  });

  it("separates replies the user sent from replies sent for them", async () => {
    render(<RecruiterStats />);

    expect(await screen.findByText("Sent for you")).toBeInTheDocument();
    expect(screen.getByText("Went out without your review")).toBeInTheDocument();
  });

  it("sums the two kinds of waiting into one number", async () => {
    render(<RecruiterStats />);

    // 6 drafts + 9 flagged.
    expect(await screen.findByText("Awaiting you")).toBeInTheDocument();
    expect(screen.getByText("15")).toBeInTheDocument();
  });

  it("switches to weekly buckets on the ninety-day range", async () => {
    const user = userEvent.setup();
    render(<RecruiterStats />);
    await screen.findByText("Scanned");

    await user.click(screen.getByRole("button", { name: "90 days" }));

    await waitFor(() =>
      expect(api.recruiterStats).toHaveBeenCalledWith({ days: 90, bucket: "week" }),
    );
  });

  it("says when push is registered but silent", async () => {
    api.recruiterStats.mockResolvedValue({
      ...PAYLOAD,
      push: { ...PAYLOAD.push, covering: false },
    });
    render(<RecruiterStats />);

    expect(
      await screen.findByText(/registered but has been quiet/),
    ).toBeInTheDocument();
  });

  it("says nothing about push on a deployment without it", async () => {
    api.recruiterStats.mockResolvedValue(EMPTY);
    render(<RecruiterStats />);

    await screen.findByText("Scanned");
    expect(screen.queryByText(/push/i)).not.toBeInTheDocument();
  });

  it("reports how scans were actually triggered", async () => {
    render(<RecruiterStats />);

    expect(await screen.findByText(/300 from push/)).toBeInTheDocument();
  });

  it("reads as up to date rather than broken when there is no history", async () => {
    api.recruiterStats.mockResolvedValue(EMPTY);
    render(<RecruiterStats />);

    expect(await screen.findByText(/No checks recorded/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing detected in this window/)).toBeInTheDocument();
  });

  it("hands a failure to the caller and renders nothing", async () => {
    const onError = vi.fn();
    api.recruiterStats.mockRejectedValue(new Error("stats are down"));

    const { container } = render(<RecruiterStats onError={onError} />);

    await waitFor(() => expect(onError).toHaveBeenCalledWith("stats are down"));
    expect(container).toBeEmptyDOMElement();
  });

  it("mentions escalated recruiters only when there are some", async () => {
    render(<RecruiterStats />);
    await screen.findByText("Scanned");
    expect(screen.queryByText(/written back/)).not.toBeInTheDocument();

    api.recruiterStats.mockResolvedValue({
      ...PAYLOAD,
      totals: { ...PAYLOAD.totals, escalated: 1 },
    });
    render(<RecruiterStats />);

    expect(await screen.findByText(/1 recruiter has written back/)).toBeInTheDocument();
  });

  it("mentions deferred messages only when the cap was hit", async () => {
    api.recruiterStats.mockResolvedValue({
      ...PAYLOAD,
      totals: { ...PAYLOAD.totals, deferred: 40 },
    });
    render(<RecruiterStats />);

    expect(await screen.findByText(/40 left for the next check/)).toBeInTheDocument();
  });
});

describe("TrendBars", () => {
  it("draws one bar per bucket", () => {
    render(<TrendBars points={PAYLOAD.trend} bucket="day" />);

    expect(screen.getAllByTestId("trend-bar")).toHaveLength(3);
  });

  it("labels the range at both ends", () => {
    render(<TrendBars points={PAYLOAD.trend} bucket="day" />);

    expect(screen.getByText("21 Jul")).toBeInTheDocument();
    expect(screen.getByText("23 Jul")).toBeInTheDocument();
  });

  it("names the peak so the bars have a scale", () => {
    render(<TrendBars points={PAYLOAD.trend} bucket="day" />);

    expect(screen.getByText("peak 11")).toBeInTheDocument();
  });

  it("is readable to a screen reader as numbers, not as a picture", () => {
    render(<TrendBars points={PAYLOAD.trend} bucket="day" />);

    expect(
      screen.getByRole("img", { name: /21 Jul 11, 22 Jul 0, 23 Jul 4/ }),
    ).toBeInTheDocument();
  });

  it("survives an all-zero window without dividing by zero", () => {
    render(
      <TrendBars
        points={[{ period: "2026-07-21", label: "21 Jul", detected: 0 }]}
        bucket="day"
      />,
    );

    expect(screen.getByText("peak 1")).toBeInTheDocument();
    expect(screen.getAllByTestId("trend-bar")).toHaveLength(1);
  });

  it("says so plainly when there is nothing to plot", () => {
    render(<TrendBars points={[]} />);

    expect(screen.getByText(/Nothing detected in this window/)).toBeInTheDocument();
    expect(screen.queryAllByTestId("trend-bar")).toHaveLength(0);
  });
});
