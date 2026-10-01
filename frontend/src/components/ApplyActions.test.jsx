import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ApplyActions, { detectAts, isLinkedInJob } from "./ApplyActions";
import { ConfirmProvider } from "./ui/ConfirmDialog";
import { ToastProvider } from "./ui/Toast";

vi.mock("../lib/api", () => ({
  api: { formApplyToJob: vi.fn(), linkedinEasyApply: vi.fn() },
}));

import { api } from "../lib/api";

const GREENHOUSE_JOB = {
  id: 7,
  company: "Acme",
  url: "https://boards.greenhouse.io/acme/jobs/123",
};

function renderActions(job) {
  return render(
    <ToastProvider>
      <ConfirmProvider>
        <ApplyActions job={job} onError={() => {}} />
      </ConfirmProvider>
    </ToastProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.formApplyToJob.mockResolvedValue({ status: "queued" });
  api.linkedinEasyApply.mockResolvedValue({ status: "queued" });
});

describe("platform detection", () => {
  it.each([
    ["https://acme.myworkdayjobs.com/en-US/careers/job/123", "Workday"],
    ["https://boards.greenhouse.io/acme/jobs/123", "Greenhouse"],
    ["https://jobs.lever.co/acme/abc-123", "Lever"],
  ])("recognises %s", (url, expected) => {
    expect(detectAts(url)).toBe(expected);
  });

  it("returns null for a plain careers page", () => {
    expect(detectAts("https://acme.com/careers/123")).toBeNull();
    expect(detectAts(null)).toBeNull();
  });

  it("recognises a LinkedIn job", () => {
    expect(isLinkedInJob("https://www.linkedin.com/jobs/view/123")).toBe(true);
    expect(isLinkedInJob("https://acme.com/jobs/1")).toBe(false);
  });
});

describe("ApplyActions", () => {
  it("offers nothing for an unrecognised URL", () => {
    // A careers page we have no adapter for gets no button at all — offering
    // one that can't work is worse than offering none.
    renderActions({ id: 1, url: "https://acme.com/careers" });
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("names the platform it is about to drive", () => {
    renderActions(GREENHOUSE_JOB);
    expect(screen.getByRole("button", { name: "Fill via Greenhouse" })).toBeInTheDocument();
  });

  it("fills without asking, because filling is reversible", async () => {
    const user = userEvent.setup();
    renderActions(GREENHOUSE_JOB);

    await user.click(screen.getByRole("button", { name: "Fill via Greenhouse" }));

    await waitFor(() =>
      expect(api.formApplyToJob).toHaveBeenCalledWith(7, { submit: false }),
    );
    // No dialog stood between the user and a run that stops before submitting.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("always confirms before submitting", async () => {
    const user = userEvent.setup();
    renderActions(GREENHOUSE_JOB);

    await user.click(screen.getByRole("button", { name: "Submit" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/can't be recalled/i);
    expect(api.formApplyToJob).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Fill in & submit" }));
    await waitFor(() =>
      expect(api.formApplyToJob).toHaveBeenCalledWith(7, { submit: true }),
    );
  });

  it("sends nothing when the submit confirmation is declined", async () => {
    const user = userEvent.setup();
    renderActions(GREENHOUSE_JOB);

    await user.click(screen.getByRole("button", { name: "Submit" }));
    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(api.formApplyToJob).not.toHaveBeenCalled();
  });

  it("confirms before a LinkedIn Easy Apply, which always submits", async () => {
    const user = userEvent.setup();
    renderActions({ id: 9, company: "Acme", url: "https://www.linkedin.com/jobs/view/9" });

    await user.click(screen.getByRole("button", { name: "Easy Apply" }));

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/LinkedIn Easy Apply/);
    expect(api.linkedinEasyApply).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Fill in & submit" }));
    await waitFor(() => expect(api.linkedinEasyApply).toHaveBeenCalledWith(9));
  });

  it("surfaces a failure instead of claiming success", async () => {
    const user = userEvent.setup();
    api.formApplyToJob.mockRejectedValue(new Error("browser unavailable"));
    renderActions(GREENHOUSE_JOB);

    await user.click(screen.getByRole("button", { name: "Fill via Greenhouse" }));
    expect(await screen.findByText("browser unavailable")).toBeInTheDocument();
  });
});
