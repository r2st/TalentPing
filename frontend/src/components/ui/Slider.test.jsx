import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Slider from "./Slider";

describe("Slider", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("debounces onCommit to the settled value", () => {
    const onCommit = vi.fn();
    render(
      <Slider min={0} max={100} value={10} onCommit={onCommit} debounce={500} aria-label="s" />,
    );
    const input = screen.getByLabelText("s");

    fireEvent.change(input, { target: { value: "40" } });
    fireEvent.change(input, { target: { value: "70" } });
    expect(onCommit).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(500));
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith(70);
  });

  it("moves the thumb immediately even before commit", () => {
    render(<Slider min={0} max={100} value={10} onCommit={() => {}} aria-label="s" />);
    const input = screen.getByLabelText("s");
    fireEvent.change(input, { target: { value: "55" } });
    expect(input).toHaveValue("55");
  });

  it("shows the formatted value bubble while interacting", () => {
    render(
      <Slider
        min={0}
        max={100}
        value={20}
        onCommit={() => {}}
        format={(v) => `${v}%`}
        aria-label="s"
      />,
    );
    const input = screen.getByLabelText("s");
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "35" } });
    expect(screen.getByText("35%")).toBeInTheDocument();
  });
});
