import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDebouncedCallback, useDebouncedValue } from "./useDebounce";

describe("useDebouncedValue", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("only updates after the delay elapses with no change", () => {
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 300), {
      initialProps: { v: "a" },
    });
    expect(result.current).toBe("a");

    rerender({ v: "ab" });
    rerender({ v: "abc" });
    expect(result.current).toBe("a"); // not yet

    act(() => vi.advanceTimersByTime(299));
    expect(result.current).toBe("a");

    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe("abc"); // latest value wins
  });
});

describe("useDebouncedCallback", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("coalesces rapid calls into one, with the latest args", () => {
    const spy = vi.fn();
    const { result } = renderHook(() => useDebouncedCallback(spy, 500));

    act(() => {
      result.current(1);
      result.current(2);
      result.current(3);
    });
    expect(spy).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(500));
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith(3);
  });

  it("can cancel a pending call", () => {
    const spy = vi.fn();
    const { result } = renderHook(() => useDebouncedCallback(spy, 500));
    act(() => {
      result.current("x");
      result.current.cancel();
      vi.advanceTimersByTime(500);
    });
    expect(spy).not.toHaveBeenCalled();
  });
});
