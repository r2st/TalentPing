import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

/**
 * App-wide confirmation dialogs, promise-style:
 *
 *   const confirm = useConfirm();
 *   if (!(await confirm({ title, message, confirmLabel, tone: "danger" }))) return;
 *
 * One dialog instance lives at the provider; `confirm()` opens it and resolves
 * true/false when the user chooses. Escape and backdrop clicks resolve false.
 */
const ConfirmContext = createContext(null);

export function ConfirmProvider({ children }) {
  const [state, setState] = useState(null);
  const resolver = useRef(null);

  const confirm = useCallback((options) => {
    return new Promise((resolve) => {
      resolver.current = resolve;
      setState(options || {});
    });
  }, []);

  const settle = useCallback((result) => {
    setState(null);
    resolver.current?.(result);
    resolver.current = null;
  }, []);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {state && (
        <ConfirmDialog
          {...state}
          onConfirm={() => settle(true)}
          onCancel={() => settle(false)}
        />
      )}
    </ConfirmContext.Provider>
  );
}

export function useConfirm() {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used within a ConfirmProvider");
  return ctx;
}

export function ConfirmDialog({
  title = "Are you sure?",
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "default",
  onConfirm,
  onCancel,
}) {
  const confirmRef = useRef(null);

  useEffect(() => {
    confirmRef.current?.focus();
    function onKey(e) {
      if (e.key === "Escape") onCancel();
      if (e.key === "Enter") onConfirm();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel, onConfirm]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onCancel}
        aria-hidden="true"
      />
      <div className="panel relative w-full max-w-sm space-y-4 p-6 shadow-lift animate-fade-up">
        <h2 className="font-display text-2xl tracking-tight text-white">{title}</h2>
        {message && (
          <p className="text-sm leading-relaxed text-white/55">{message}</p>
        )}
        <div className="flex items-center justify-end gap-2 pt-1">
          <button className="btn-ghost" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            className={tone === "danger" ? "btn btn-danger" : "btn-primary"}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
