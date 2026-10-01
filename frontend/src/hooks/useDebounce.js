import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Returns a debounced copy of `value` that only updates once `delay` ms have
 * passed without a change. Use for driving a fetch off a fast-changing input
 * (a text filter, a slider) without a request per keystroke or tick.
 */
export function useDebouncedValue(value, delay = 300) {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return debounced;
}

/**
 * Wraps `fn` so calls coalesce: the wrapped function only fires `delay` ms after
 * the last invocation. The latest arguments win. Always calls the current `fn`,
 * so it's safe to pass an inline closure. Also returns a `.cancel()` to drop a
 * pending call (e.g. on unmount).
 */
export function useDebouncedCallback(fn, delay = 300) {
  const timer = useRef(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const cancel = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => cancel, [cancel]);

  const debounced = useCallback(
    (...args) => {
      cancel();
      timer.current = setTimeout(() => {
        timer.current = null;
        fnRef.current(...args);
      }, delay);
    },
    [cancel, delay],
  );

  debounced.cancel = cancel;
  return debounced;
}
