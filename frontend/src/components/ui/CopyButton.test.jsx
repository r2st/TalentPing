import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import CopyButton from "./CopyButton";

// Patch only navigator.clipboard (not the whole navigator, which user-event
// depends on) and restore it afterwards.
function withClipboard(writeText) {
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
  return () => {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else delete navigator.clipboard;
  };
}

describe("CopyButton", () => {
  afterEach(() => vi.restoreAllMocks());

  it("copies a plain string and flips the label", async () => {
    const writeText = vi.fn().mockResolvedValue();
    const restore = withClipboard(writeText);

    render(<CopyButton text="hello" label="Copy" copiedLabel="Copied" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("hello"));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    restore();
  });

  it("resolves an async text producer before copying", async () => {
    const writeText = vi.fn().mockResolvedValue();
    const restore = withClipboard(writeText);

    render(<CopyButton text={() => Promise.resolve("fetched markdown")} label="Copy" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("fetched markdown"));
    restore();
  });

  it("reports failures through onError", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    const restore = withClipboard(writeText);
    const onError = vi.fn();

    render(<CopyButton text="x" onError={onError} />);
    fireEvent.click(screen.getByRole("button"));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("denied"));
    restore();
  });
});
