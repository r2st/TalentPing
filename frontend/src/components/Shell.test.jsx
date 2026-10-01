import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { notifyCountsChanged } from "../lib/events";
import Shell from "./Shell";

vi.mock("../lib/api", () => ({
  api: { review: vi.fn(), inbox: vi.fn(), onboarding: vi.fn() },
}));

vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({ user: { email: "candidate@example.com" }, logout: vi.fn() }),
}));

import { api } from "../lib/api";

function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/pipeline"]}>
      <Shell>
        <p>page body</p>
      </Shell>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.review.mockResolvedValue({ count: 0, items: [] });
  api.inbox.mockResolvedValue({ counts: { threads: 0, unread: 0 }, threads: [] });
  api.onboarding.mockResolvedValue({ complete: true });
});

describe("Shell navigation", () => {
  it("offers exactly the four destinations", async () => {
    renderShell();
    await waitFor(() => expect(api.inbox).toHaveBeenCalled());

    for (const [label, href] of [
      ["Pipeline", "/pipeline"],
      ["Jobs", "/jobs"],
      ["Inbox", "/inbox"],
      ["Setup", "/setup"],
    ]) {
      expect(screen.getAllByRole("link", { name: new RegExp(label) })[0]).toHaveAttribute(
        "href",
        href,
      );
    }
  });

  it("drops Review and Tailor as destinations of their own", async () => {
    renderShell();
    await waitFor(() => expect(api.inbox).toHaveBeenCalled());

    expect(screen.queryByRole("link", { name: /Review/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Tailor/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Autopilot/ })).not.toBeInTheDocument();
  });

  it("keeps Setup in the nav after onboarding is finished", async () => {
    /* It used to become an unlabelled gear here, leaving the page that holds
       every resume and preference with no named route to it. */
    api.onboarding.mockResolvedValue({ complete: true });
    renderShell();

    const setup = await screen.findByRole("link", { name: /^Setup$/ });
    expect(setup).toHaveAttribute("href", "/setup");
  });

  it("keeps Setup in the nav when the onboarding read fails", async () => {
    /* The old hook assumed "finished" on error, which hid Setup precisely when
       something was already wrong. Nothing about the nav depends on that read
       now, so it cannot happen. */
    api.onboarding.mockRejectedValue(new Error("offline"));
    renderShell();

    expect(await screen.findByRole("link", { name: /^Setup$/ })).toHaveAttribute(
      "href",
      "/setup",
    );
  });

  it("reaches Setup from the phone menu too", async () => {
    const user = userEvent.setup();
    renderShell();
    await waitFor(() => expect(api.inbox).toHaveBeenCalled());

    await user.click(screen.getByLabelText("Toggle navigation menu"));

    // Both the inline nav and the open menu render it; neither may be missing.
    expect(screen.getAllByRole("link", { name: /^Setup$/ })).toHaveLength(2);
  });

  it("badges the Inbox with everything waiting on the user", async () => {
    api.inbox.mockResolvedValue({ counts: { threads: 4, unread: 3 }, threads: [] });
    api.review.mockResolvedValue({ count: 2, items: [] });
    renderShell();

    // Three unread replies plus two drafts — one page now, so one number.
    expect(await screen.findByLabelText("5 waiting on you")).toBeInTheDocument();
  });

  it("shows no badge when both queues are empty", async () => {
    renderShell();
    await waitFor(() => expect(api.inbox).toHaveBeenCalled());

    expect(screen.queryByLabelText(/waiting on you/)).not.toBeInTheDocument();
  });

  it("refreshes the badge as soon as a page reports a change", async () => {
    renderShell();
    await waitFor(() => expect(api.inbox).toHaveBeenCalledTimes(1));

    api.inbox.mockResolvedValue({ counts: { threads: 1, unread: 1 }, threads: [] });
    notifyCountsChanged();

    // Without the signal the header would keep the stale count for 30 seconds.
    expect(await screen.findByLabelText("1 waiting on you")).toBeInTheDocument();
  });

  it("survives a queue request failing", async () => {
    api.inbox.mockRejectedValue(new Error("offline"));
    api.review.mockRejectedValue(new Error("offline"));
    renderShell();

    // The chrome still renders; only the badges are missing.
    await waitFor(() => expect(api.inbox).toHaveBeenCalled());
    expect(screen.getByText("page body")).toBeInTheDocument();
    expect(screen.queryByLabelText(/waiting on you/)).not.toBeInTheDocument();
  });

  it("keeps the nav usable when the queue reads fail", async () => {
    api.review.mockRejectedValue(new Error("offline"));
    renderShell();

    await waitFor(() => expect(api.review).toHaveBeenCalled());
    expect(screen.getAllByRole("link", { name: /Pipeline/ })[0]).toBeInTheDocument();
  });
});
