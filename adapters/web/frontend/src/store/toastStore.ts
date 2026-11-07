// [2026-11-07] Global operation-feedback notifications (toast).
// Why: settings pages used per-page `message` strings rendered at the top of the
// page or inside cards, which scroll out of view, and ContextPopover kept its own
// local toast. How: one zustand queue plus a `notify` facade callable from any
// component or plain module; <Toaster /> is mounted once at the App root.
// Purpose: every success/failure feedback goes through this single path.
import { create } from 'zustand';

export type ToastKind = 'success' | 'error' | 'info';

export interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
  /** Auto-dismiss delay in milliseconds. */
  duration: number;
}

const DEFAULT_DURATION: Record<ToastKind, number> = {
  success: 3500,
  info: 3500,
  error: 8000,
};

/** Keep the stack short so a burst of failures cannot cover the screen. */
const MAX_TOASTS = 5;

interface ToastStore {
  toasts: ToastItem[];
  push: (kind: ToastKind, text: string, duration?: number) => number;
  dismiss: (id: number) => void;
}

let toastSeq = 0;

export const useToastStore = create<ToastStore>((set) => ({
  toasts: [],
  push: (kind, text, duration) => {
    const id = ++toastSeq;
    const item: ToastItem = { id, kind, text, duration: duration ?? DEFAULT_DURATION[kind] };
    set((state) => ({ toasts: [...state.toasts, item].slice(-MAX_TOASTS) }));
    return id;
  },
  dismiss: (id) => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
}));

/** Extract a readable message from an unknown thrown value. */
export const errorText = (err: unknown, fallback: string): string => {
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === 'string' && err) return err;
  return fallback;
};

const push = (kind: ToastKind) => (text: string, duration?: number) =>
  useToastStore.getState().push(kind, text, duration);

/**
 * Global notification API.
 *   notify.success('已保存')
 *   notify.error(errorText(err, '保存失败'))
 *   notify.info('已提交请求')
 */
export const notify = {
  success: push('success'),
  error: push('error'),
  info: push('info'),
  dismiss: (id: number) => useToastStore.getState().dismiss(id),
};
