// [2026-06-01] Application view registry for chat and settings modes.
// Why: App.tsx should choose a view by key instead of branching over every slot.
// How: each AppViewDefinition supplies left sidebar, header, main, optional composer,
// and right-column slots as render functions. Purpose: adding a future app view or
// settings page does not create a root-level if-else chain.
import type { ReactNode } from 'react';

import { ChatInput } from '../components/chat';
import { MessageListV2 } from '../components/chat/v2';
import { SystemDashboard } from '../components/dashboard/SystemDashboard';
import { Header, Sidebar } from '../components/layout';
import { EventLogPanel } from '../components/log';
import { SettingsHeader } from '../components/settings/SettingsHeader';
import { SettingsPageHost } from '../components/settings/SettingsPageHost';
import { SettingsRightPanel } from '../components/settings/SettingsRightPanel';
import { SettingsSidebar } from '../components/settings/SettingsSidebar';
import type { ConversationMeta } from '../store/chatStoreV2';
import type { ViewMode } from '../store/viewStore';
import type { Attachment } from '../types';
import type { ToolExecution, WsMessage } from '../types/message';

export interface AppViewContext {
  sessionId: string;
  title: string;
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  messages: WsMessage[];
  toolsById: Record<string, ToolExecution>;
  isGenerating: boolean;
  onCreateConversation: () => void;
  onSelectConversation: (conversationId: string) => void;
  onDeleteConversation: (conversationId: string) => void;
  onSendMessage: (text: string, attachments?: Attachment[]) => Promise<void> | void;
  onCancel: () => void;
  onReset: () => void;
}

export interface AppViewDefinition {
  id: string;
  sidebar: (ctx: AppViewContext) => ReactNode;
  header: (ctx: AppViewContext) => ReactNode;
  main: (ctx: AppViewContext) => ReactNode;
  composer?: (ctx: AppViewContext) => ReactNode;
  rightTop?: (ctx: AppViewContext) => ReactNode;
  rightBottom?: (ctx: AppViewContext) => ReactNode;
}

const safeSessionId = (sessionId: string) => sessionId || 'no-session';

export const viewRegistry: Record<ViewMode, AppViewDefinition> = {
  chat: {
    id: 'chat',
    sidebar: (ctx) => (
      <Sidebar
        activeConversationId={ctx.activeConversationId}
        conversations={ctx.conversations}
        onCreateConversation={ctx.onCreateConversation}
        onDeleteConversation={ctx.onDeleteConversation}
        onSelectConversation={ctx.onSelectConversation}
      />
    ),
    header: (ctx) => (
      <Header
        isGenerating={ctx.isGenerating}
        onCancel={ctx.onCancel}
        onReset={ctx.onReset}
        sessionId={safeSessionId(ctx.sessionId)}
        title={ctx.title}
      />
    ),
    main: (ctx) => <MessageListV2 messages={ctx.messages} toolsById={ctx.toolsById} />,
    composer: (ctx) => <ChatInput disabled={ctx.isGenerating} onSend={ctx.onSendMessage} />,
    // [2026-06-01] Keep the chat right rail focused on system status.
    // Why: the session editor now opens from Header as an overlay modal. How:
    // render SystemDashboard in the upper right slot for every chat view. Purpose:
    // General operational counters remain visible while users chat.
    rightTop: () => <SystemDashboard />,
    rightBottom: () => <EventLogPanel />,
  },
  settings: {
    id: 'settings',
    sidebar: () => <SettingsSidebar />,
    header: () => <SettingsHeader />,
    main: () => <SettingsPageHost />,
    rightTop: () => <SettingsRightPanel />,
    // [2026-06-02] Settings no longer reserves a lower EventLog slot. Why: contextual
    // settings editors need the full right rail, especially on narrow screens. How:
    // leave rightBottom undefined for settings while chat keeps EventLogPanel. Purpose:
    // AppLayout can promote SettingsRightPanel to full height without view-specific CSS.
  },
};
