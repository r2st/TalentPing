import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";
import Embed from "../Embed";

describe("Embed", () => {
  it("renders the embed page with tool selector", () => {
    render(<MemoryRouter><Embed /></MemoryRouter>);
    expect(screen.getByText("Embed Our Tools")).toBeInTheDocument();
    expect(screen.getByText("Embed Code")).toBeInTheDocument();
  });

  it("generates an iframe code snippet", () => {
    render(<MemoryRouter><Embed /></MemoryRouter>);
    expect(screen.getByText(/iframe/)).toBeInTheDocument();
  });

  it("shows copy code button", () => {
    render(<MemoryRouter><Embed /></MemoryRouter>);
    expect(screen.getByText("Copy Code")).toBeInTheDocument();
  });

  it("copies embed code on click", async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    render(<MemoryRouter><Embed /></MemoryRouter>);
    fireEvent.click(screen.getByText("Copy Code"));
    expect(await screen.findByText("Copied!")).toBeInTheDocument();
  });
});
