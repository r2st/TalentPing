import { useEffect, useRef, useState } from "react";

/**
 * Copy-to-clipboard button with a brief "Copied" acknowledgement. Falls back to
 * a hidden textarea + execCommand where the async clipboard API is unavailable
 * (older browsers, insecure origins).
 */
export default function CopyButton({
  text,
  label = "Copy",
  copiedLabel = "Copied",
  className = "btn-quiet",
  onError,
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  async function copy() {
    try {
      // `text` may be a string, or a (possibly async) function that produces one
      // — e.g. fetching the rendered resume markdown on demand.
      const value = await (typeof text === "function" ? text() : text);
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        fallbackCopy(value);
      }
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1600);
    } catch (err) {
      onError?.(err.message || "Couldn't copy to clipboard.");
    }
  }

  return (
    <button className={className} onClick={copy} aria-live="polite">
      {copied ? copiedLabel : label}
    </button>
  );
}

function fallbackCopy(value) {
  const el = document.createElement("textarea");
  el.value = value;
  el.setAttribute("readonly", "");
  el.style.position = "absolute";
  el.style.left = "-9999px";
  document.body.appendChild(el);
  el.select();
  document.execCommand("copy");
  document.body.removeChild(el);
}
