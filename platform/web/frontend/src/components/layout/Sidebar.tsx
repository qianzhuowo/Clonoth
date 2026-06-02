// [2026-05-16] Upgraded: connection status from settingsStore, settings toggle, delete conversation.
// [2026-05-31] Step 3 accepts V2 ConversationMeta rows instead of legacy
// Conversation objects. Why: chatStoreV2 keeps message bodies normalized outside the
// sidebar list. How: render title, session preview, and updated time without reading a
// messages array. Purpose: let App switch stores without changing sidebar behavior.
import type { ConversationMeta } from '../../store/chatStoreV2';
import { useSettingsStore } from '../../store/settingsStore';
import { useViewStore } from '../../store/viewStore';
import { Button, Icon } from '../common';

interface SidebarProps {
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  onCreateConversation: () => void;
  onSelectConversation: (conversationId: string) => void;
  onDeleteConversation: (conversationId: string) => void;
}

const formatTime = (isoDate: string) =>
  new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(isoDate));

export const Sidebar = ({
  conversations, activeConversationId,
  onCreateConversation, onSelectConversation, onDeleteConversation,
}: SidebarProps) => {
  const isConnected = useSettingsStore(state => state.isConnected);
  const openSettings = useViewStore(state => state.openSettings);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="border-b border-[var(--duties-border)] p-3">
        <p className="font-mono text-[0.6rem] uppercase tracking-[0.22em] text-[var(--duties-tertiary)]">Clonoth</p>
        <h1 className="mt-1.5 font-mono text-lg font-semibold tracking-[-0.04em]">Clonoth 网页端</h1>
        <p className="mt-1.5 text-xs text-[var(--duties-secondary)]">调度器网页界面</p>
        <Button className="mt-3 w-full" onClick={onCreateConversation} variant="primary">
          新对话
        </Button>
      </div>

      {/* Conversation list */}
      <nav aria-label="对话" className="min-h-0 flex-1 overflow-y-auto">
        {conversations.length === 0 && (
          <p className="p-3 text-center text-xs text-[var(--duties-tertiary)]">暂无对话</p>
        )}
        {conversations.map((conv) => {
          const isActive = conv.id === activeConversationId;
          return (
            <div
              className={`group relative border-b border-[var(--duties-border)] transition-colors hover:bg-[var(--duties-accent)] ${
                isActive ? 'bg-[var(--duties-muted)]' : 'bg-transparent'
              }`}
              key={conv.id}
            >
              <button
                className="block w-full p-3 text-left focus:outline-none"
                onClick={() => onSelectConversation(conv.id)}
                type="button"
              >
                <span className="block font-mono text-xs font-semibold">{conv.title}</span>
                <span className="mt-1.5 block truncate text-[0.7rem] text-[var(--duties-tertiary)]">
                  {conv.sessionId ? `会话 ${conv.sessionId.slice(0, 8)}` : '暂无会话'}
                </span>
                <span className="mt-2 block font-mono text-[0.6rem] uppercase tracking-[0.16em] text-[var(--duties-tertiary)]">
                  {formatTime(conv.updatedAt)}
                </span>
              </button>
              {/* Delete button — visible on hover (desktop) or always visible (mobile).
                  [2026-06-01] Why: remove the literal close glyph used for deletion.
                  How: render the Material Symbols delete icon through Icon. Purpose:
                  destructive conversation controls are visually clear and consistent. */}
              <button
                className="absolute right-2 top-2 text-xs text-[var(--duties-tertiary)] hover:text-red-500 md:hidden md:group-hover:block"
                onClick={(e) => { e.stopPropagation(); onDeleteConversation(conv.id); }}
                title="删除对话"
                type="button"
              >
                <Icon name="delete" size={15} />
              </button>
            </div>
          );
        })}
      </nav>

      {/* Bottom — connection status + settings */}
      <div className="border-t border-[var(--duties-border)] p-3">
        <div className="flex items-center gap-2 text-[0.7rem] text-[var(--duties-tertiary)]">
          <span className={`inline-block h-1.5 w-1.5 rounded-full ${isConnected ? 'bg-green-500' : 'bg-red-500'}`} />
          {isConnected ? '已连接' : '已断开'}
        </div>
        <button
          className="mt-2 flex w-full items-center gap-2 px-2 py-1.5 text-xs text-[var(--duties-secondary)] transition-colors hover:bg-[var(--duties-muted)]"
          onClick={() => {
            // [2026-06-01] Settings is now a full registered view, not a legacy
            // right-panel toggle. Why: the left sidebar must be replaced by
            // settings categories while the right panel remains independent. How:
            // route through viewStore.openSettings(). Purpose: AppLayout receives
            // settings slots from viewRegistry without Sidebar knowing the details.
            openSettings();
          }}
          type="button"
        >
          {/* [2026-06-01] Why: replace the settings gear emoji with Material Symbols.
              How: render the shared Icon using the settings symbol. Purpose: sidebar
              actions stay on the same icon system as layout and message controls. */}
          <Icon name="settings" size={16} />
          <span>设置</span>
        </button>
      </div>
    </div>
  );
};
