import { useEffect } from "react";
import { createPortal } from "react-dom";

/**
 * A document, open over the page.
 *
 * Shared rather than per-screen: the inbox needed it first — a PDF is not
 * something you can judge from its name, and approving a reply is irreversible —
 * and setup needs exactly the same thing for exactly the same reason. A resume
 * the product describes back to you as a headline and a filename is a resume you
 * cannot check.
 *
 * Rendered in an iframe off a blob URL rather than linked at the API path,
 * because those endpoints are behind a bearer token that a plain `href` wouldn't
 * carry. The caller does the fetching and owns the URLs' lifetime; this only
 * draws them.
 *
 * Two URLs, because a Word document cannot be both read and saved off one. `url`
 * is the file — what Download hands over, and what the frame shows for anything
 * a browser can draw. `previewUrl` is the server's rendering of a format it
 * can't, and it is only ever framed: it is a picture of the document, the
 * document is what travels to recruiters, and the strip above the frame says so
 * rather than leaving the user to assume one from the other.
 *
 * Portalled to `document.body`, and that is load-bearing rather than tidiness.
 * `position: fixed` resolves against the viewport only while no ancestor has
 * established a containing block, and any non-`none` `backdrop-filter` does
 * exactly that — which `.panel` sets via `backdrop-blur-sm`. Rendered in place,
 * this overlay laid itself out inside the conversation panel and was then
 * clipped by that panel's `overflow-hidden` and the message list's own scroll
 * box: the fetch succeeded, the blob was fine, and the preview was a sliver
 * nobody could see. Escaping the panel is what makes it open over the page.
 */

// Formats no browser will render in a frame. Office documents are a zip of XML:
// pointed at one, a browser either downloads it behind the overlay or draws
// nothing at all, and "nothing at all" is indistinguishable from a broken
// preview. These are the files a `previewUrl` is fetched for — and, when the
// server has no rendering to give, the ones named as a download instead.
const UNRENDERABLE = /\.(docx?|xlsx?|pptx?|od[tsp]|rtf|pages)$/i;

/** Whether a file of this name can be shown in place, rather than downloaded. */
export function isRenderableInBrowser(name) {
  return !UNRENDERABLE.test(name || "");
}

/** How a Word file is described when it is the thing being talked about. */
function formatOf(name) {
  return /\.docx?$/i.test(name || "") ? "Word document" : "file";
}

export default function FilePreview({
  name,
  url,
  previewUrl = null,
  onClose,
  label = "Attached",
}) {
  useEffect(() => {
    function onKey(event) {
      if (event.key !== "Escape") return;
      // Captured, and stopped dead. The inbox binds Escape at the page level to
      // clear the thread selection; without this, closing the preview would
      // also close the conversation underneath it.
      event.stopImmediatePropagation();
      event.preventDefault();
      onClose();
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const renderable = isRenderableInBrowser(name);
  const converted = !renderable && Boolean(previewUrl);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Preview of ${name}`}
    >
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />
      <div className="panel relative flex h-[85vh] w-full max-w-4xl flex-col overflow-hidden shadow-lift animate-fade-up">
        <div className="flex items-center gap-3 border-b px-5 py-3 hairline">
          <span className="eyebrow">{label}</span>
          <span className="min-w-0 truncate font-mono text-xs text-white/60">
            {name}
          </span>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {/* Always the original, never the rendering: the file the user saves
                has to be the file the recruiter opens. */}
            <a className="btn-ghost" href={url} download={name}>
              Download
            </a>
            <button type="button" className="btn-ghost" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
        {converted && (
          /* Said in our own chrome rather than inside the frame, where it would
             be one more thing in a document the user is trying to read — and
             where a document could imitate it. A rendering is close enough to
             check the right file is going out and not the file itself, and the
             difference is the whole reason this line is here. */
          <p className="border-b px-5 py-2 text-[11px] leading-relaxed text-white/45 hairline">
            Showing a preview of this {formatOf(name)}. Formatting may differ —
            recruiters receive the original file, unchanged.
          </p>
        )}
        {renderable || converted ? (
          /* Bare white: a PDF viewer draws its own page, and the app's dark
             surface behind a document reads as a rendering fault. */
          <iframe
            title={`Preview of ${name}`}
            src={converted ? previewUrl : url}
            className="min-h-0 flex-1 bg-white"
            /* Only the conversion is sandboxed, and it is sandboxed even though
               the server builds that HTML itself and escapes every text node
               out of the document: this is a page assembled from a file a
               stranger may have emailed. No scripts, no same-origin. Popups are
               allowed so a link in the document can open where it belongs
               instead of replacing the document being read. */
            sandbox={converted ? "allow-popups allow-popups-to-escape-sandbox" : undefined}
          />
        ) : (
          <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
            <p className="text-sm text-white/70">
              This {formatOf(name)} can't be shown in the browser.
            </p>
            <a className="btn-primary" href={url} download={name}>
              Download to read it
            </a>
            <p className="text-xs text-white/35">
              It travels to recruiters exactly as it is — this is the file
              itself, not a conversion of it.
            </p>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
