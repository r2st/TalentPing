import { createContext, useCallback, useContext, useRef, useState } from "react";

/**
 * App-wide toast notifications.
 *
 *   const toast = useToast();
 *   toast.success("Preferences saved");
 *   toast.error(err.message);
 *   toast.show({ message, tone: "info", duration: 4000 });
 *
 * Toasts stack bottom-right and auto-dismiss; errors linger longer and can be
 * dismissed by hand. Mount <ToastProvider> once near the app root.
 */
const ToastContext = createContext(null);

const TONES = {
  success: "border-good/30 bg-good/[0.12] text-good",
  error: "border-bad/30 bg-bad/[0.12] text-bad",
  info: "border-signal/30 bg-signal/[0.12] text-signal",
};

let counter = 0;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const timers = useRef(new Map());

  const dismiss = useCallback((id) => {
    setToasts((list) => list.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const show = useCallback(
    ({ message, tone = "info", duration }) => {
      if (!message) return undefined;
      const id = ++counter;
      const ttl = duration ?? (tone === "error" ? 6000 : 3500);
      setToasts((list) => [...list, { id, message, tone }]);
      if (ttl > 0) {
        timers.current.set(
          id,
          setTimeout(() => dismiss(id), ttl),
        );
      }
      return id;
    },
    [dismiss],
  );

  const api = useRef(null);
  if (!api.current) {
    api.current = {
      show,
      dismiss,
      success: (message, opts) => show({ ...opts, message, tone: "success" }),
      error: (message, opts) => show({ ...opts, message, tone: "error" }),
      info: (message, opts) => show({ ...opts, message, tone: "info" }),
    };
  }

  return (
    <ToastContext.Provider value={api.current}>
      {children}
      <div
        className="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex flex-col items-center gap-2 p-4 sm:items-end sm:p-6"
        aria-live="polite"
        aria-atomic="false"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.tone === "error" ? "alert" : "status"}
            className={[
              "pointer-events-auto flex w-full max-w-sm items-start justify-between gap-3 rounded-lg border px-4 py-3",
              "text-sm shadow-lift backdrop-blur-sm animate-fade-up",
              TONES[t.tone] ?? TONES.info,
            ].join(" ")}
          >
            <span className="min-w-0 break-words">{t.message}</span>
            <button
              className="shrink-0 opacity-60 transition-opacity hover:opacity-100"
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss notification"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within a ToastProvider");
  return ctx;
}
