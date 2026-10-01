import { SHORTCUT_HINTS } from "../../hooks/useKeyboard";

/**
 * The quiet strip that tells you the keyboard shortcuts exist. Shortcuts nobody
 * knows about are shortcuts nobody uses, and a legend is cheaper than a tour.
 * Hidden on touch layouts, where there is no keyboard to hint at.
 */
export default function ShortcutHints({ hints = SHORTCUT_HINTS, className = "" }) {
  return (
    <p
      className={["hidden flex-wrap items-center gap-3 sm:flex", className].join(" ")}
      aria-label="Keyboard shortcuts"
    >
      {hints.map(([keys, label]) => (
        <span key={keys} className="flex items-center gap-1.5">
          <kbd className="rounded border border-white/10 bg-white/[0.04] px-1.5 py-0.5 font-mono text-[10px] text-white/45">
            {keys}
          </kbd>
          <span className="text-[11px] text-white/25">{label}</span>
        </span>
      ))}
    </p>
  );
}
