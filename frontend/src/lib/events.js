// A one-line pub/sub for "the queue counts changed".
//
// The nav badges poll on a timer, so reading a reply or approving a draft would
// otherwise leave a stale count in the header for up to 30 seconds. Pages that
// change a count fire this; the Shell listens and refetches immediately.

const EVENT = "talentping:counts-changed";

export function notifyCountsChanged() {
  window.dispatchEvent(new Event(EVENT));
}

/** Subscribe; returns the unsubscribe function for an effect cleanup. */
export function onCountsChanged(handler) {
  window.addEventListener(EVENT, handler);
  return () => window.removeEventListener(EVENT, handler);
}
