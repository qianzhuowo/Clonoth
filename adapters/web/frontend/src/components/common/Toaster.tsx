// [2026-11-07] Global toast renderer, mounted exactly once at the App root.
// Why: operation feedback must stay visible regardless of page scroll position.
// How: render the toastStore queue in a fixed stack above every modal (Modal uses
// z-50); each item owns its auto-dismiss timer and pauses it while hovered or
// focused. Layout: full-width under the top edge on phones, top-right on desktop.
import { useEffect, useRef, useState } from 'react';

import { useToastStore, type ToastItem, type ToastKind } from '../../store/toastStore';

const KIND_STYLE: Record<ToastKind, { box: string; bar: string; icon: string; label: string }> = {
  success: {
    box: 'border-[var(--duties-border)] bg-[var(--duties-panel)] text-[var(--duties-text)]',
    bar: 'bg-emerald-500',
    icon: '✓',
    label: '成功',
  },
  info: {
    box: 'border-[var(--duties-border)] bg-[var(--duties-panel)] text-[var(--duties-text)]',
    bar: 'bg-[var(--duties-accent)]',
    icon: 'i',
    label: '提示',
  },
  error: {
    box: 'border-red-300 bg-red-50 text-red-700',
    bar: 'bg-red-500',
    icon: '!',
    label: '错误',
  },
};

const ToastCard = ({ toast }: { toast: ToastItem }) => {
  const dismiss = useToastStore((state) => state.dismiss);
  const [paused, setPaused] = useState(false);
  const remainingRef = useRef(toast.duration);
  const startedAtRef = useRef(0);

  useEffect(() => {
    // Run the remaining time while not paused; on pause, bank the elapsed time.
    if (paused) return;
    startedAtRef.current = Date.now();
    const timer = setTimeout(() => dismiss(toast.id), Math.max(0, remainingRef.current));
    return () => {
      clearTimeout(timer);
      remainingRef.current -= Date.now() - startedAtRef.current;
    };
  }, [paused, dismiss, toast.id]);

  const style = KIND_STYLE[toast.kind];
  const isError = toast.kind === 'error';

  return (
    <div
      aria-atomic="true"
      className={`clonoth-toast pointer-events-auto relative flex w-full items-start gap-2 overflow-hidden rounded-md border py-2 pl-3 pr-2 text-xs leading-5 shadow-lg ${style.box}`}
      onBlur={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      role={isError ? 'alert' : 'status'}
    >
      <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-1 ${style.bar}`} />
      <span
        aria-hidden="true"
        className={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full font-mono text-[0.6rem] font-bold text-white ${style.bar}`}
      >
        {style.icon}
      </span>
      <span className="sr-only">{style.label}：</span>
      <p className="min-w-0 flex-1 whitespace-pre-wrap break-words">{toast.text}</p>
      <button
        aria-label="关闭通知"
        className={`-my-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded font-mono text-sm opacity-60 transition-opacity hover:opacity-100 focus:opacity-100 focus:outline-none ${isError ? 'hover:bg-red-100' : 'hover:bg-[var(--duties-muted)]'}`}
        onClick={() => dismiss(toast.id)}
        type="button"
      >
        ×
      </button>
    </div>
  );
};

export const Toaster = () => {
  const toasts = useToastStore((state) => state.toasts);

  return (
    <div
      aria-live="polite"
      aria-relevant="additions"
      className="pointer-events-none fixed inset-x-3 top-[max(0.75rem,env(safe-area-inset-top))] z-[1000] flex flex-col gap-2 sm:inset-x-auto sm:right-4 sm:top-4 sm:w-[360px]"
    >
      {toasts.map((toast) => <ToastCard key={toast.id} toast={toast} />)}
    </div>
  );
};
