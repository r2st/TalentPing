import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Tooltip from "./Tooltip";

describe("Tooltip", () => {
  it("renders the label alongside the wrapped control", () => {
    render(
      <Tooltip label="Connect Gmail first">
        <button disabled>Run now</button>
      </Tooltip>,
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent("Connect Gmail first");
    expect(screen.getByRole("button", { name: "Run now" })).toBeDisabled();
  });

  it("renders children unwrapped when there is no label", () => {
    render(
      <Tooltip label="">
        <button>Run now</button>
      </Tooltip>,
    );
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(screen.getByRole("button", { name: "Run now" })).toBeInTheDocument();
  });
});
