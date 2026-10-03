import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect } from "vitest";
import ToolsIndex from "../ToolsIndex";

describe("ToolsIndex", () => {
  it("renders all tool cards", () => {
    render(<MemoryRouter><ToolsIndex /></MemoryRouter>);
    expect(screen.getByText("ATS Resume Score Checker")).toBeInTheDocument();
    expect(screen.getByText("Tech Salary Estimator")).toBeInTheDocument();
    expect(screen.getByText("Cover Letter Generator")).toBeInTheDocument();
  });

  it("renders tool descriptions", () => {
    render(<MemoryRouter><ToolsIndex /></MemoryRouter>);
    expect(screen.getByText(/paste your resume/i)).toBeInTheDocument();
    expect(screen.getByText(/salary ranges/i)).toBeInTheDocument();
  });
});
