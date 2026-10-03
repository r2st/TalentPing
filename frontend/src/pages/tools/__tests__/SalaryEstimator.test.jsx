import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect } from "vitest";
import SalaryEstimator from "../SalaryEstimator";

function renderPage() {
  return render(<MemoryRouter><SalaryEstimator /></MemoryRouter>);
}

describe("SalaryEstimator", () => {
  it("renders heading and three dropdowns", () => {
    renderPage();
    expect(screen.getByText("Tech Salary Estimator")).toBeInTheDocument();
    expect(screen.getByText("Select role")).toBeInTheDocument();
    expect(screen.getByText("Select location")).toBeInTheDocument();
    expect(screen.getByText("Select experience")).toBeInTheDocument();
  });

  it("shows salary range when all selections made", () => {
    renderPage();
    fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "Software Engineer" } });
    fireEvent.change(screen.getAllByRole("combobox")[1], { target: { value: "San Francisco, CA" } });
    fireEvent.change(screen.getAllByRole("combobox")[2], { target: { value: "3-5 years" } });
    expect(screen.getByText("Estimated Salary Range")).toBeInTheDocument();
    expect(screen.getByText("Median")).toBeInTheDocument();
  });

  it("does not show salary range without all selections", () => {
    renderPage();
    fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "Software Engineer" } });
    expect(screen.queryByText("Estimated Salary Range")).not.toBeInTheDocument();
  });
});
