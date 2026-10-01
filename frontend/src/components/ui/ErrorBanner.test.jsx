import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ErrorBanner from "./ErrorBanner";

describe("ErrorBanner", () => {
  it("renders nothing when there is no message", () => {
    const { container } = render(<ErrorBanner>{null}</ErrorBanner>);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the message with an alert role for errors", () => {
    render(<ErrorBanner>Something broke</ErrorBanner>);
    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("Something broke");
  });

  it("uses a status role for non-error tones", () => {
    render(<ErrorBanner tone="signal">Heads up</ErrorBanner>);
    expect(screen.getByRole("status")).toHaveTextContent("Heads up");
  });

  it("fires onDismiss when the close button is clicked", () => {
    const onDismiss = vi.fn();
    render(<ErrorBanner onDismiss={onDismiss}>Oops</ErrorBanner>);
    fireEvent.click(screen.getByLabelText("Dismiss"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("omits the close button without onDismiss", () => {
    render(<ErrorBanner>Oops</ErrorBanner>);
    expect(screen.queryByLabelText("Dismiss")).toBeNull();
  });
});
