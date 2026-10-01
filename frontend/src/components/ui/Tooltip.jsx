/**
 * A lightweight hover/focus tooltip. Wraps its children in a relatively
 * positioned span so it works even around a *disabled* control (which fires no
 * pointer events of its own — the wrapper catches the hover instead).
 *
 *   <Tooltip label="Connect Gmail first"><button disabled>Run now</button></Tooltip>
 *
 * Pass an empty/falsy `label` to render the children with no tooltip at all.
 */
export default function Tooltip({ label, children, side = "top", className = "" }) {
  if (!label) return children;

  const position =
    side === "bottom"
      ? "top-full mt-2"
      : side === "left"
        ? "right-full mr-2 top-1/2 -translate-y-1/2"
        : side === "right"
          ? "left-full ml-2 top-1/2 -translate-y-1/2"
          : "bottom-full mb-2";

  const horizontal = side === "top" || side === "bottom" ? "left-1/2 -translate-x-1/2" : "";

  return (
    <span className={["group relative inline-flex", className].join(" ")}>
      {children}
      <span
        role="tooltip"
        className={[
          "pointer-events-none absolute z-40 whitespace-nowrap rounded-md border bg-ink-700 px-2.5 py-1.5",
          "font-mono text-[11px] text-white/80 shadow-lift hairline",
          "opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100",
          position,
          horizontal,
        ].join(" ")}
      >
        {label}
      </span>
    </span>
  );
}
