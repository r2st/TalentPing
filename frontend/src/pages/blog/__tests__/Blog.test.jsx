import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, it, expect } from "vitest";
import BlogIndex from "../BlogIndex";
import BlogPost from "../BlogPost";

describe("BlogIndex", () => {
  it("renders all article cards", () => {
    render(<MemoryRouter><BlogIndex /></MemoryRouter>);
    expect(screen.getByText("How AI is Revolutionizing the Job Application Process")).toBeInTheDocument();
    expect(screen.getByText("5 Resume Mistakes That Get You Rejected by ATS Systems")).toBeInTheDocument();
    expect(screen.getByText("The Complete Guide to Automated Job Applications")).toBeInTheDocument();
  });

  it("renders the blog heading", () => {
    render(<MemoryRouter><BlogIndex /></MemoryRouter>);
    expect(screen.getByRole("heading", { level: 1, name: "Blog" })).toBeInTheDocument();
  });
});

describe("BlogPost", () => {
  it("renders article content for a valid slug", () => {
    render(
      <MemoryRouter initialEntries={["/blog/ai-revolutionizing-job-applications"]}>
        <Routes>
          <Route path="/blog/:slug" element={<BlogPost />} />
        </Routes>
      </MemoryRouter>
    );
    expect(screen.getByText("How AI is Revolutionizing the Job Application Process")).toBeInTheDocument();
  });

  it("shows not found for an unknown slug", () => {
    render(
      <MemoryRouter initialEntries={["/blog/nonexistent-article"]}>
        <Routes>
          <Route path="/blog/:slug" element={<BlogPost />} />
        </Routes>
      </MemoryRouter>
    );
    expect(screen.getByText("Article Not Found")).toBeInTheDocument();
  });

  it("renders share buttons on articles", () => {
    render(
      <MemoryRouter initialEntries={["/blog/resume-mistakes-ats-rejection"]}>
        <Routes>
          <Route path="/blog/:slug" element={<BlogPost />} />
        </Routes>
      </MemoryRouter>
    );
    expect(screen.getByText("WhatsApp")).toBeInTheDocument();
    expect(screen.getByText("Twitter/X")).toBeInTheDocument();
    expect(screen.getByText("Copy Link")).toBeInTheDocument();
  });
});
