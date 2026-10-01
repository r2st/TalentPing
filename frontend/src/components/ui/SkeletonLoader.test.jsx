import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import SkeletonLoader from "./SkeletonLoader";

describe("SkeletonLoader", () => {
  it("renders `count` blocks by default", () => {
    render(<SkeletonLoader count={4} />);
    expect(screen.getByTestId("skeleton-loader").children).toHaveLength(4);
  });

  it("renders one block per entry in `rows` with those heights", () => {
    render(<SkeletonLoader rows={[24, 56, 64]} />);
    const blocks = screen.getByTestId("skeleton-loader").children;
    expect(blocks).toHaveLength(3);
    expect(blocks[0]).toHaveStyle({ height: "24px" });
    expect(blocks[2]).toHaveStyle({ height: "64px" });
  });

  it("uses the shimmer treatment when asked", () => {
    render(<SkeletonLoader rows={[40]} shimmer />);
    const block = screen.getByTestId("skeleton-loader").firstChild;
    // The shimmer variant nests a sweeping highlight; the pulse variant doesn't.
    expect(block.querySelector(".animate-shimmer")).not.toBeNull();
  });
});
