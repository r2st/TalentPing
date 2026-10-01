/**
 * The TalentPing mark: a person at the centre, the ping going out both ways.
 *
 * Colour is `currentColor`, so it takes the amber from whatever wraps it rather
 * than hard-coding the accent — the same mark then works on an inverted surface
 * without a second file. The favicons carry a simplified version of this shape
 * (solid core, one pair of arcs) because the person doesn't survive 16px.
 */
export default function Logo({ className = "h-6 w-6", title }) {
  return (
    <svg
      viewBox="0 0 48 48"
      className={className}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : "true"}
    >
      {title && <title>{title}</title>}
      <g fill="none" stroke="currentColor" strokeLinecap="round">
        <path d="M30.95 37.07A14.8 14.8 0 0 0 30.95 10.93" strokeWidth="4" />
        <path d="M17.05 10.93A14.8 14.8 0 0 0 17.05 37.07" strokeWidth="4" />
        <path d="M36.31 39.76A20 20 0 0 0 36.31 8.24" strokeWidth="3.5" />
        <path d="M11.69 8.24A20 20 0 0 0 11.69 39.76" strokeWidth="3.5" />
      </g>
      {/* One path, even-odd filled: the disc with the head and shoulders cut
          out of it, so the person is a hole and not a second colour. */}
      <path
        fill="currentColor"
        fillRule="evenodd"
        d="M24 14.3a9.7 9.7 0 1 1 0 19.4a9.7 9.7 0 1 1 0-19.4Z M24 18.24a2.86 2.86 0 1 1 0 5.72a2.86 2.86 0 1 1 0-5.72Z M19.1 30.5a4.9 4.9 0 0 1 9.8 0Z"
      />
    </svg>
  );
}
