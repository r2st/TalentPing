import { describe, expect, it, vi } from "vitest";
import { formatWhen } from "./format";

describe("formatWhen", () => {
  it("returns a dash for empty values", () => {
    expect(formatWhen(null)).toBe("—");
    expect(formatWhen(undefined)).toBe("—");
    expect(formatWhen("")).toBe("—");
  });

  it("reads recent past as 'ago'", () => {
    const now = new Date("2026-07-24T12:00:00Z");
    vi.useFakeTimers();
    vi.setSystemTime(now);
    expect(formatWhen(new Date(now.getTime() - 30 * 60000))).toBe("30m ago");
    expect(formatWhen(new Date(now.getTime() - 3 * 3600 * 1000))).toBe("3h ago");
    expect(formatWhen(new Date(now.getTime() - 2 * 86400 * 1000))).toBe("2d ago");
    vi.useRealTimers();
  });

  it("reads the near future as 'in'", () => {
    const now = new Date("2026-07-24T12:00:00Z");
    vi.useFakeTimers();
    vi.setSystemTime(now);
    expect(formatWhen(new Date(now.getTime() + 45 * 60000))).toBe("in 45m");
    expect(formatWhen(new Date(now.getTime() + 3 * 86400 * 1000))).toBe("in 3d");
    vi.useRealTimers();
  });

  it("falls back to a calendar date once relative time stops helping", () => {
    const now = new Date("2026-07-24T12:00:00Z");
    vi.useFakeTimers();
    vi.setSystemTime(now);
    // This year: no year needed.
    const thisYear = formatWhen(new Date("2026-03-02T12:00:00Z"));
    expect(thisYear).toMatch(/Mar/);
    expect(thisYear).not.toMatch(/2026/);
    // Older mail carries the year — the inbox now shows a whole history, and
    // "Jun 20" on a two-year-old email reads as last month.
    expect(formatWhen(new Date("2024-06-20T12:00:00Z"))).toMatch(/2024/);
    vi.useRealTimers();
  });

  it("collapses sub-minute differences to 'just now'", () => {
    const now = new Date("2026-07-24T12:00:00Z");
    vi.useFakeTimers();
    vi.setSystemTime(now);
    expect(formatWhen(new Date(now.getTime() - 20 * 1000))).toBe("just now");
    vi.useRealTimers();
  });
});
