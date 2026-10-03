import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import ShareButtons from "../ShareButtons";

describe("ShareButtons", () => {
  it("renders all three share buttons", () => {
    render(<ShareButtons url="https://example.com" title="Test" />);
    expect(screen.getByText("WhatsApp")).toBeInTheDocument();
    expect(screen.getByText("Twitter/X")).toBeInTheDocument();
    expect(screen.getByText("Copy Link")).toBeInTheDocument();
  });

  it("shows Copied! when copy link is clicked", async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    render(<ShareButtons url="https://example.com" title="Test" />);
    fireEvent.click(screen.getByText("Copy Link"));
    expect(await screen.findByText("Copied!")).toBeInTheDocument();
  });

  it("WhatsApp link contains the url", () => {
    render(<ShareButtons url="https://test.com/page" title="My Title" />);
    const link = screen.getByText("WhatsApp").closest("a");
    expect(link.href).toContain("wa.me");
    expect(link.href).toContain(encodeURIComponent("https://test.com/page"));
  });

  it("Twitter link contains the title", () => {
    render(<ShareButtons url="https://test.com" title="My Title" />);
    const link = screen.getByText("Twitter/X").closest("a");
    expect(link.href).toContain("twitter.com");
    expect(link.href).toContain(encodeURIComponent("My Title"));
  });
});
