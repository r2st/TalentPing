import { useState } from "react";

/**
 * Comma/Enter/blur-committed tag input — the one used for roles, locations and
 * keywords everywhere. Manages its own draft; the parent only owns `values`.
 *
 * Commit splits on commas, trims, drops blanks, and skips case-insensitive
 * duplicates. `suggested` tints the field and chips so a pre-filled value reads
 * as something to review rather than something the user typed.
 */
export default function TagInput({
  id,
  placeholder,
  values = [],
  onChange,
  suggested = false,
  disabled = false,
  addLabel = "Add",
}) {
  const [draft, setDraft] = useState("");

  function commit() {
    const names = draft
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean);
    if (!names.length) return;
    const next = [...values];
    for (const name of names) {
      if (!next.some((v) => v.toLowerCase() === name.toLowerCase())) next.push(name);
    }
    onChange(next);
    setDraft("");
  }

  function remove(value) {
    onChange(values.filter((v) => v !== value));
  }

  return (
    <div>
      <div className="flex gap-2">
        <input
          id={id}
          className={["input", suggested && "input-suggested"].filter(Boolean).join(" ")}
          value={draft}
          disabled={disabled}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              commit();
            }
          }}
          onBlur={commit}
          placeholder={placeholder}
        />
        <button
          className="btn-ghost shrink-0"
          onClick={commit}
          disabled={disabled || !draft.trim()}
        >
          {addLabel}
        </button>
      </div>

      {values.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {values.map((value) => (
            <span
              key={value}
              className={suggested ? "chip border-signal/35 text-signal-soft" : "chip"}
            >
              {value}
              <button
                className="ml-0.5 text-white/30 hover:text-bad"
                onClick={() => remove(value)}
                aria-label={`Remove ${value}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
