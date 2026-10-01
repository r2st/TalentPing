import { useEffect, useState } from "react";
import { isRenderableInBrowser } from "./FilePreview";

/**
 * Open a document in `FilePreview`, fetching whatever it takes to show it.
 *
 * Two screens do this — setup opens a resume, the inbox opens an email's
 * attachment — and both need the same two-part answer, because a Word document
 * cannot be both read and downloaded off one URL:
 *
 * - **The file itself**, always. It is what the Download control hands over, and
 *   it is what the frame renders whenever the browser can draw it. Nothing here
 *   substitutes a conversion for it: what the user saves is what the recruiter
 *   receives.
 * - **A server-side rendering**, only for the formats a browser will not draw.
 *   One extra request, and only for a .docx — a PDF costs exactly what it did
 *   before this existed.
 *
 * Both are fetched at once rather than in sequence: they are independent, and a
 * preview that took two round trips to open would feel like the slower of them
 * twice. A rendering that fails is not a failure to open the document — the
 * overlay opens anyway and offers the file, which is the behaviour that existed
 * before conversion was possible at all.
 *
 * The blobs outlive the fetch and are released when the preview closes or the
 * caller unmounts, not when `open` returns.
 */
export default function useFilePreview({ name, fetchFile, fetchPreview, onError }) {
  const [urls, setUrls] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!urls) return undefined;
    return () => {
      URL.revokeObjectURL(urls.url);
      if (urls.previewUrl) URL.revokeObjectURL(urls.previewUrl);
    };
  }, [urls]);

  async function open() {
    if (busy) return;
    setBusy(true);
    try {
      const convert = Boolean(fetchPreview) && !isRenderableInBrowser(name);
      const [file, rendering] = await Promise.all([
        fetchFile(),
        // Swallowed on purpose, and only here: the file is the document, the
        // rendering is a convenience, and 409 "this can't be shown" is an
        // answer the overlay already knows how to display.
        convert ? fetchPreview().catch(() => null) : Promise.resolve(null),
      ]);
      setUrls({
        url: URL.createObjectURL(file),
        previewUrl: rendering ? URL.createObjectURL(rendering) : null,
      });
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  }

  return {
    busy,
    open,
    close: () => setUrls(null),
    url: urls?.url ?? null,
    previewUrl: urls?.previewUrl ?? null,
  };
}
