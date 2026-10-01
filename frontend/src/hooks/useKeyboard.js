import { useCallback, useEffect, useRef } from "react";

/**
 * Keyboard shortcuts, with the one rule that makes them safe to add anywhere:
 * a keystroke aimed at a text field is never a shortcut.
 *
 * Without that guard, typing "reply to Jordan" in the search box would fire `r`
 * and `j` on every character. So we ignore any event whose target is an input,
 * textarea, select or contenteditable — except Escape, which every field should
 * be able to close its surroundings with.
 */

const TEXT_ENTRY = new Set(["INPUT", "TEXTAREA", "SELECT"]);

/** True when the event came from somewhere the user is typing. */
export function isTyping(event) {
  const el = event.target;
  if (!el) return false;
  return TEXT_ENTRY.has(el.tagName) || el.isContentEditable === true;
}

/**
 * Bind a `{ key: handler }` map for as long as the component is mounted.
 *
 * Keys are matched case-sensitively against `event.key`, so "j", "Enter" and
 * "Escape" all work. A handler returning nothing still counts as handled: the
 * event is consumed so the browser doesn't also scroll on space or navigate on
 * backspace. Pass `enabled: false` to suspend the whole map (a modal is open,
 * the list is empty) without unmounting anything.
 */
export function useKeyboard(handlers, { enabled = true } = {}) {
  // Held in a ref so re-rendering with fresh closures doesn't re-bind the
  // listener on every keystroke.
  const ref = useRef(handlers);
  ref.current = handlers;

  useEffect(() => {
    if (!enabled) return undefined;

    function onKeyDown(event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const handler = ref.current?.[event.key];
      if (typeof handler !== "function") return;
      // Escape is the exception: it has to work from inside a field, because
      // that is exactly where a user reaches for it.
      if (isTyping(event) && event.key !== "Escape") return;
      event.preventDefault();
      handler(event);
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}

/**
 * j/k/Enter/Escape over a list, tracking the cursor for you.
 *
 * `items` is the rendered order, `selectedId` the current selection (or null),
 * and `onSelect` moves it. j/k step through and select as they go — the same
 * "moving the cursor opens the row" behaviour as a mail client, which is what
 * makes it worth keeping a hand on the keyboard at all.
 */
export function useListKeyboard({
  items,
  selectedId,
  idOf = (item) => item.id,
  onSelect,
  onOpen,
  onClose,
  onReply,
  enabled = true,
}) {
  const step = useCallback(
    (delta) => {
      if (!items.length) return;
      const current = items.findIndex((item) => idOf(item) === selectedId);
      // Nothing selected yet: j starts at the top, k at the bottom.
      const next =
        current === -1
          ? delta > 0
            ? 0
            : items.length - 1
          : Math.min(Math.max(current + delta, 0), items.length - 1);
      onSelect?.(idOf(items[next]));
    },
    [items, selectedId, idOf, onSelect],
  );

  useKeyboard(
    {
      j: () => step(1),
      k: () => step(-1),
      Enter: () => {
        if (selectedId != null) onOpen?.(selectedId);
        else step(1);
      },
      Escape: () => onClose?.(),
      r: () => selectedId != null && onReply?.(selectedId),
    },
    { enabled },
  );
}

/** The shortcuts a page advertises, for the hint strip. */
export const SHORTCUT_HINTS = [
  ["j / k", "move"],
  ["enter", "open"],
  ["r", "reply"],
  ["esc", "close"],
];
