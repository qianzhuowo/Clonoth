// [2026-06-01] Modal wrapper for session configuration.
// Why: node and model editing should no longer replace the chat right rail, which
// now hosts the persistent system dashboard. How: render SessionConfigPanel inside a
// centered fixed overlay and close from the backdrop or close button. Purpose: users
// can adjust the current session without losing dashboard visibility.
import { SessionConfigPanel } from './SessionConfigPanel';
import { Icon } from '../common';

interface SessionConfigModalProps {
  sessionId: string;
  focus: 'node' | 'model';
  onClose: () => void;
}

export const SessionConfigModal = ({ sessionId, focus, onClose }: SessionConfigModalProps) => {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={onClose}>
      <div
        aria-label="会话配置"
        aria-modal="true"
        className="flex max-h-[86dvh] w-full max-w-lg flex-col border border-[var(--duties-border)] bg-[var(--duties-panel)] shadow-xl"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="flex items-center justify-between border-b border-[var(--duties-border)] px-3 py-2">
          <div>
            <p className="font-mono text-[0.55rem] uppercase tracking-[0.18em] text-[var(--duties-tertiary)]">会话编辑</p>
            <h2 className="font-mono text-sm font-semibold tracking-[-0.03em]">
              {focus === 'node' ? '节点配置' : '模型配置'}
            </h2>
          </div>
          <button
            aria-label="关闭会话配置"
            className="rounded-sm p-1 text-[var(--duties-tertiary)] transition-colors hover:bg-[var(--duties-muted)] hover:text-[var(--duties-text)]"
            onClick={onClose}
            type="button"
          >
            {/* [2026-06-01] Why: the modal needs an explicit close affordance.
                How: use the shared Material Symbols icon instead of a text glyph.
                Purpose: modal controls stay consistent with the frontend icon set. */}
            <Icon name="close" size={18} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden">
          <SessionConfigPanel focus={focus} sessionId={sessionId} />
        </div>
      </div>
    </div>
  );
};
