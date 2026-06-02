// [2026-05-17] Header: node display from backend (source of truth).
// [2026-05-31] Step 3 renames the activity prop from isTyping to isGenerating.
// Why: V2 no longer has typingConversationId or streamPreview state. How: the header
// consumes the store-level generation flag directly. Purpose: keep cancel/reset UI
// aligned with the reducer-backed chat flow.
import { useEffect, useState } from 'react';

import { getActiveNode, getAppConfig, getNodes } from '../../api/supervisorClient';
import { useSettingsStore } from '../../store/settingsStore';
import { Button, Icon } from '../common';
import { SessionConfigModal } from '../settings/SessionConfigModal';

interface HeaderProps {
  title: string;
  sessionId: string;
  isGenerating: boolean;
  onCancel?: () => void;
  onReset?: () => void;
}

export const Header = ({ title, sessionId, isGenerating, onCancel, onReset }: HeaderProps) => {
  const {
    adminToken, availableNodes, activeNodeId, entryNodeId, globalModel, sessionProviderOverride,
    setActiveNode, setGlobalConfig, setAvailableNodes,
  } = useSettingsStore();
  const [configModalFocus, setConfigModalFocus] = useState<'node' | 'model' | null>(null);

  const displayNodeId = activeNodeId || entryNodeId;
  const activeNode = availableNodes.find(n => n.id === displayNodeId);
  const nodeModel = (activeNode as any)?.model || '';
  const sessionModel = typeof sessionProviderOverride?.model === 'string' ? sessionProviderOverride.model : '';
  const displayModel = sessionModel || nodeModel || globalModel || '(默认)';

  const openSessionConfigModal = (focus: 'node' | 'model') => {
    // [2026-06-01] Header labels now open a focused session configuration modal.
    // Why: the chat right rail is reserved for the persistent SystemDashboard. How:
    // keep a local modal focus state instead of routing through settings view or a
    // right-panel override. Purpose: node/model edits do not disturb dashboard state.
    setConfigModalFocus(focus);
  };

  // Fetch active node from backend when sessionId changes — backend is source of truth
  useEffect(() => {
    if (!sessionId || sessionId === 'no-session') return;
    getActiveNode(sessionId)
      .then(r => {
        setActiveNode(r.node_id, r.is_override, r.default_node_id);
      })
      .catch(() => {});
  }, [sessionId, setActiveNode]);

  // Fetch global config once
  useEffect(() => {
    getAppConfig()
      .then(r => setGlobalConfig(r.openai?.model || '', r.openai?.base_url || ''))
      .catch(() => {});
  }, [setGlobalConfig]);

  // Load nodes if needed
  useEffect(() => {
    if (availableNodes.length > 0 || !adminToken) return;
    getNodes(adminToken)
      .then(n => setAvailableNodes(n.filter((nd: any) => nd.type === 'ai' && !nd.id.startsWith('system.'))))
      .catch(() => {});
  }, [adminToken, availableNodes.length, setAvailableNodes]);

  return (
    <>
      <header className="px-3 py-2 sm:px-4 sm:py-3">
      <div className="mx-auto flex max-w-3xl items-center justify-between gap-2">
        {/* Left: title + badges */}
        <div className="min-w-0 flex-1">
          <h2 className="truncate font-mono text-sm font-semibold tracking-[-0.03em]">{title}</h2>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 font-mono text-[0.6rem] text-[var(--duties-tertiary)]">
            <span
              className="cursor-pointer transition-colors hover:text-[var(--duties-text)]"
              onClick={() => openSessionConfigModal('node')}
              title="切换节点"
            >
              <span className="inline-flex items-center gap-1">
                {/* [2026-06-01] Why: replace the node hexagon glyph with Material Symbols.
                    How: render the shared Icon using the hub symbol. Purpose: header
                    badges share the same visual icon system as settings tabs. */}
                <Icon name="hub" size={13} />
                <span>{activeNode?.name || displayNodeId || '选择节点'}</span>
              </span>
            </span>
            <span className="text-[var(--duties-border)]">/</span>
            <span
              className="cursor-pointer transition-colors hover:text-[var(--duties-text)]"
              onClick={() => openSessionConfigModal('model')}
              title="模型配置"
            >
              <span className="inline-flex items-center gap-1">
                {/* [2026-06-01] Why: replace the model antenna emoji with Material Symbols.
                    How: render the model_training symbol through Icon. Purpose: model
                    badges are font-based icons instead of emoji. */}
                <Icon name="model_training" size={13} />
                <span>{displayModel}</span>
              </span>
            </span>
          </div>
        </div>

        {/* Right: action buttons */}
        <div className="flex items-center gap-2">
          {isGenerating && onCancel && (
            <Button className="h-7 px-2 text-[0.6rem]" onClick={onCancel} variant="ghost">
              {/* [2026-06-01] Why: remove the stop-sign emoji from the cancel control.
                  How: use the Material Symbols cancel icon before the text. Purpose:
                  header actions remain accessible text plus consistent iconography. */}
              <Icon name="cancel" size={14} /> 取消
            </Button>
          )}
          {!isGenerating && onReset && (
            <Button className="h-7 px-2 text-[0.6rem]" onClick={onReset} variant="ghost" title="重置对话">
              {/* [2026-06-01] Why: remove the reset emoji from the header action.
                  How: render refresh through Material Symbols. Purpose: all header
                  action icons are drawn from the shared font. */}
              <Icon name="refresh" size={14} />
            </Button>
          )}
        </div>
      </div>
    </header>
      {configModalFocus && (
        <SessionConfigModal
          focus={configModalFocus}
          onClose={() => setConfigModalFocus(null)}
          sessionId={sessionId}
        />
      )}
    </>
  );
};
