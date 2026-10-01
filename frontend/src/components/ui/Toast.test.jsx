import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToastProvider, useToast } from "./Toast";

function Trigger() {
  const toast = useToast();
  return (
    <div>
      <button onClick={() => toast.success("Saved")}>ok</button>
      <button onClick={() => toast.error("Broke")}>bad</button>
    </div>
  );
}

function setup() {
  return render(
    <ToastProvider>
      <Trigger />
    </ToastProvider>,
  );
}

describe("Toast / useToast", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("shows a success toast and auto-dismisses it", () => {
    setup();
    act(() => screen.getByText("ok").click());
    expect(screen.getByRole("status")).toHaveTextContent("Saved");

    act(() => vi.advanceTimersByTime(3500));
    expect(screen.queryByText("Saved")).toBeNull();
  });

  it("shows an error toast with an alert role that lingers longer", () => {
    setup();
    act(() => screen.getByText("bad").click());
    expect(screen.getByRole("alert")).toHaveTextContent("Broke");

    act(() => vi.advanceTimersByTime(3500));
    expect(screen.getByText("Broke")).toBeInTheDocument(); // still up at 3.5s

    act(() => vi.advanceTimersByTime(2500));
    expect(screen.queryByText("Broke")).toBeNull(); // gone by 6s
  });

  it("dismisses by hand via the close button", () => {
    setup();
    act(() => screen.getByText("ok").click());
    act(() => screen.getByLabelText("Dismiss notification").click());
    expect(screen.queryByText("Saved")).toBeNull();
  });
});
