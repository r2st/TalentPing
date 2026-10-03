import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";
import ResumeScore from "../ResumeScore";

function renderPage() {
  return render(<MemoryRouter><ResumeScore /></MemoryRouter>);
}

describe("ResumeScore", () => {
  it("renders the heading and textarea", () => {
    renderPage();
    expect(screen.getByText("ATS Resume Score Checker")).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/paste your resume/i)).toBeInTheDocument();
  });

  it("disables analyze button when textarea is empty", () => {
    renderPage();
    expect(screen.getByText("Analyze Resume")).toBeDisabled();
  });

  it("calculates a score for a sample resume", () => {
    renderPage();
    const textarea = screen.getByPlaceholderText(/paste your resume/i);
    fireEvent.change(textarea, {
      target: {
        value: "Experience: Led a team of 12 engineers. Managed $2M budget. Achieved 30% growth. Education: BS Computer Science. Skills: Python, React, Node.js. Contact: jane@example.com Phone: 555-123-4567",
      },
    });
    fireEvent.click(screen.getByText("Analyze Resume"));
    expect(screen.getByText(/strong|needs work|weak/i)).toBeInTheDocument();
  });

  it("shows improvement tips", () => {
    renderPage();
    const textarea = screen.getByPlaceholderText(/paste your resume/i);
    fireEvent.change(textarea, { target: { value: "short resume" } });
    fireEvent.click(screen.getByText("Analyze Resume"));
    expect(screen.getByText("Tips to Improve")).toBeInTheDocument();
  });
});
