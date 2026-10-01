import { useEffect, useState } from "react";
import { useDebouncedCallback } from "../../hooks/useDebounce";

/**
 * A range slider that tracks its thumb with a value bubble and debounces the
 * commit — so dragging from 40 to 90 shows every value live but only saves
 * once the user settles, instead of firing a request per tick.
 *
 *   <Slider min={40} max={95} step={5} value={score}
 *           onCommit={(v) => save({ min_fit_score: v })}
 *           format={(v) => `${v}%`} />
 */
export default function Slider({
  min = 0,
  max = 100,
  step = 1,
  value,
  onCommit,
  debounce = 500,
  format = (v) => v,
  disabled = false,
  id,
  "aria-label": ariaLabel,
}) {
  // Local value drives the thumb and bubble immediately; the commit is deferred.
  const [local, setLocal] = useState(value);
  const [active, setActive] = useState(false);

  // Re-sync when the parent's value changes from outside a drag (e.g. a refresh).
  useEffect(() => {
    setLocal(value);
  }, [value]);

  const commit = useDebouncedCallback((v) => onCommit(v), debounce);

  function handleChange(e) {
    const next = Number(e.target.value);
    setLocal(next);
    commit(next);
  }

  const pct = max === min ? 0 : ((local - min) / (max - min)) * 100;

  return (
    <div className="relative pt-7">
      <span
        className={[
          "pointer-events-none absolute top-0 -translate-x-1/2 rounded-md border bg-ink-700 px-2 py-0.5",
          "font-mono text-[11px] text-signal shadow-lift hairline transition-opacity duration-150",
          active ? "opacity-100" : "opacity-0",
        ].join(" ")}
        // The thumb is ~16px wide; nudge the bubble so it tracks the centre at
        // both ends instead of overhanging.
        style={{ left: `calc(${pct}% + ${8 - pct * 0.16}px)` }}
        aria-hidden="true"
      >
        {format(local)}
      </span>
      <input
        id={id}
        aria-label={ariaLabel}
        type="range"
        min={min}
        max={max}
        step={step}
        value={local}
        disabled={disabled}
        onChange={handleChange}
        onPointerDown={() => setActive(true)}
        onPointerUp={() => setActive(false)}
        onFocus={() => setActive(true)}
        onBlur={() => setActive(false)}
        onMouseEnter={() => setActive(true)}
        onMouseLeave={() => setActive(false)}
        className="w-full accent-signal"
      />
    </div>
  );
}
