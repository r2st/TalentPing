import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import FilePreview, { isRenderableInBrowser } from "./FilePreview";

/**
 * The overlay two screens now share: the inbox opens an email's attachment in
 * it, setup opens a resume. These pin the behaviour both depend on — it opens
 * over the page rather than inside whatever panel rendered it, Escape closes
 * this and nothing behind it, and a file the browser cannot draw says so.
 */

describe("FilePreview", () => {
  it("renders the document in place", () => {
    render(
      <FilePreview name="backend.pdf" url="blob:doc" onClose={() => {}} />,
    );

    const dialog = screen.getByRole("dialog", { name: "Preview of backend.pdf" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByTitle("Preview of backend.pdf")).toHaveAttribute(
      "src",
      "blob:doc",
    );
  });

  it("escapes the panel that rendered it", () => {
    // The regression this guards: `position: fixed` only resolves against the
    // viewport while no ancestor has established a containing block, and any
    // non-`none` `backdrop-filter` does — which `.panel` sets via
    // `backdrop-blur-sm`. Rendered in place, the overlay laid itself out inside
    // its panel and was clipped by that panel's `overflow-hidden`. The fetch
    // worked, the blob was fine, and the preview was a sliver nobody could see.
    const { container } = render(
      <div className="panel overflow-hidden">
        <FilePreview name="backend.pdf" url="blob:doc" onClose={() => {}} />
      </div>,
    );

    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(document.body).toContainElement(screen.getByRole("dialog"));
  });

  it("closes on the button, the backdrop, and Escape", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<FilePreview name="backend.pdf" url="blob:doc" onClose={onClose} />);

    await user.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(
      screen.getByRole("dialog").querySelector('[aria-hidden="true"]'),
    );
    fireEvent.keyDown(window, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("stops Escape reaching the page underneath", async () => {
    // The inbox binds Escape at the page level to clear the thread selection.
    // Without the capture-phase stop, closing the preview also closed the
    // conversation behind it.
    const behind = vi.fn();
    window.addEventListener("keydown", behind);
    render(<FilePreview name="backend.pdf" url="blob:doc" onClose={() => {}} />);

    fireEvent.keyDown(window, { key: "Escape" });
    window.removeEventListener("keydown", behind);

    expect(behind).not.toHaveBeenCalled();
  });

  it("leaves other keys to the page", () => {
    const behind = vi.fn();
    window.addEventListener("keydown", behind);
    render(<FilePreview name="backend.pdf" url="blob:doc" onClose={() => {}} />);

    fireEvent.keyDown(window, { key: "j" });
    window.removeEventListener("keydown", behind);

    expect(behind).toHaveBeenCalled();
  });

  it("offers a download of the file under its own name", () => {
    render(<FilePreview name="backend.pdf" url="blob:doc" onClose={() => {}} />);

    const link = screen.getByRole("link", { name: "Download" });
    expect(link).toHaveAttribute("href", "blob:doc");
    expect(link).toHaveAttribute("download", "backend.pdf");
  });

  it("labels what the document is, so one overlay reads right on both screens", () => {
    render(
      <FilePreview
        name="backend.pdf"
        url="blob:doc"
        label="Resume"
        onClose={() => {}}
      />,
    );

    expect(screen.getByText("Resume")).toBeInTheDocument();
  });

  it("says a Word document can't be drawn instead of framing nothing", () => {
    // No rendering to show: the server couldn't convert it, or wasn't asked.
    render(<FilePreview name="cv.docx" url="blob:doc" onClose={() => {}} />);

    expect(screen.getByText(/can't be shown in the browser/)).toBeInTheDocument();
    expect(screen.queryByTitle("Preview of cv.docx")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download to read it" })).toHaveAttribute(
      "download",
      "cv.docx",
    );
  });

  it("frames the server's rendering of a Word document when there is one", () => {
    render(
      <FilePreview
        name="cv.docx"
        url="blob:doc"
        previewUrl="blob:rendering"
        onClose={() => {}}
      />,
    );

    expect(screen.getByTitle("Preview of cv.docx")).toHaveAttribute(
      "src",
      "blob:rendering",
    );
    expect(screen.queryByText(/can't be shown in the browser/)).not.toBeInTheDocument();
  });

  it("downloads the document even while showing a rendering of it", () => {
    // The invariant the whole feature balances on: the rendering is for reading,
    // the file is what travels to recruiters, and the two must not swap places.
    render(
      <FilePreview
        name="cv.docx"
        url="blob:doc"
        previewUrl="blob:rendering"
        onClose={() => {}}
      />,
    );

    const link = screen.getByRole("link", { name: "Download" });
    expect(link).toHaveAttribute("href", "blob:doc");
    expect(link).toHaveAttribute("download", "cv.docx");
  });

  it("says the frame is a preview, in our chrome rather than in the document", () => {
    // Inside the frame it would be one more thing in a document the user is
    // trying to read — and a document could imitate it.
    render(
      <FilePreview
        name="cv.docx"
        url="blob:doc"
        previewUrl="blob:rendering"
        onClose={() => {}}
      />,
    );

    expect(
      screen.getByText(/Showing a preview of this Word document/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/recruiters receive the original file, unchanged/),
    ).toBeInTheDocument();
  });

  it("sandboxes a rendering, and only a rendering", () => {
    // The HTML is built server-side from a document a stranger may have emailed.
    // No scripts, no same-origin; popups so a link in the document can open
    // where it belongs instead of replacing what is being read.
    const { unmount } = render(
      <FilePreview
        name="cv.docx"
        url="blob:doc"
        previewUrl="blob:rendering"
        onClose={() => {}}
      />,
    );
    expect(screen.getByTitle("Preview of cv.docx")).toHaveAttribute(
      "sandbox",
      "allow-popups allow-popups-to-escape-sandbox",
    );
    unmount();

    render(<FilePreview name="cv.pdf" url="blob:doc" onClose={() => {}} />);
    // A PDF is the browser's own viewer, and sandboxing it has broken that
    // viewer in shipping browsers.
    expect(screen.getByTitle("Preview of cv.pdf")).not.toHaveAttribute("sandbox");
  });

  it("ignores a rendering for a document the browser draws itself", () => {
    // Nothing should be fetching one, and if something did, the PDF is still the
    // better thing to show: it is the file.
    render(
      <FilePreview
        name="cv.pdf"
        url="blob:doc"
        previewUrl="blob:rendering"
        onClose={() => {}}
      />,
    );

    expect(screen.getByTitle("Preview of cv.pdf")).toHaveAttribute("src", "blob:doc");
  });
});

describe("isRenderableInBrowser", () => {
  it("frames what browsers can draw", () => {
    expect(isRenderableInBrowser("resume.pdf")).toBe(true);
    expect(isRenderableInBrowser("scan.PDF")).toBe(true);
    expect(isRenderableInBrowser("portfolio.png")).toBe(true);
  });

  it("refuses the Office formats, whatever their case", () => {
    // What decides whether a rendering is fetched at all, so it has to agree
    // with the server's own list of formats a browser will not draw.
    expect(isRenderableInBrowser("cv.docx")).toBe(false);
    expect(isRenderableInBrowser("cv.DOC")).toBe(false);
    expect(isRenderableInBrowser("rates.xlsx")).toBe(false);
    expect(isRenderableInBrowser("deck.pptx")).toBe(false);
    expect(isRenderableInBrowser("cv.odt")).toBe(false);
    expect(isRenderableInBrowser("cv.rtf")).toBe(false);
  });

  it("treats a nameless file as something to try drawing", () => {
    // The inbox lists a sender's attachment by whatever name arrived, which may
    // be nothing at all. A frame that draws nothing is recoverable; refusing to
    // frame a PDF because it came in unnamed is not.
    expect(isRenderableInBrowser(undefined)).toBe(true);
    expect(isRenderableInBrowser("")).toBe(true);
  });
});
