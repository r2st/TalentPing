import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";
import CoverLetterGenerator from "../CoverLetterGenerator";

function renderPage() {
  return render(<MemoryRouter><CoverLetterGenerator /></MemoryRouter>);
}

describe("CoverLetterGenerator", () => {
  it("renders heading and form fields", () => {
    renderPage();
    expect(screen.getByText("Cover Letter Generator")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Jane Smith")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Acme Corp")).toBeInTheDocument();
  });

  it("generates a cover letter", () => {
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("Jane Smith"), { target: { value: "Jane" } });
    fireEvent.change(screen.getByPlaceholderText("Acme Corp"), { target: { value: "TestCo" } });
    fireEvent.change(screen.getByPlaceholderText("Senior Software Engineer"), { target: { value: "Engineer" } });
    fireEvent.change(screen.getByPlaceholderText("React, TypeScript, Node.js"), { target: { value: "React, Node" } });
    fireEvent.click(screen.getByText("Generate Cover Letter"));
    expect(screen.getByText("Your Cover Letter")).toBeInTheDocument();
    expect(screen.getByText(/Dear Hiring Manager/)).toBeInTheDocument();
  });

  it("includes company name in generated letter", () => {
    renderPage();
    fireEvent.change(screen.getByPlaceholderText("Acme Corp"), { target: { value: "FooCorp" } });
    fireEvent.change(screen.getByPlaceholderText("Senior Software Engineer"), { target: { value: "Dev" } });
    fireEvent.change(screen.getByPlaceholderText("React, TypeScript, Node.js"), { target: { value: "Python" } });
    fireEvent.click(screen.getByText("Generate Cover Letter"));
    expect(screen.getByText(/FooCorp/)).toBeInTheDocument();
  });
});
