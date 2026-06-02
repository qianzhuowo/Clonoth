// [2026-05-31] Reducer-backed chat store for the Step 2A frontend refactor.
// Why: the legacy store mixes WebSocket transport, stream previews, and rendered
// messages in one mutable path. How: keep conversation/session actions here while
// every SupervisorEvent is replayed through eventReducer into ChatState. Purpose:
// let the new message model run beside the old store until the UI migration is done.
import { create } from 'zustand';

import { connectSessionWS, disconnectSessionWS } from '../api';
import {
  cancelActiveTasks,
  decideApproval,
  deleteSession,
  getSessionHistory,
  listSessions,
  pollEvents,
  postInbound,
  uploadAttachment,
  type StructuredMessage,
} from '../api/supervisorClient';
import type { SupervisorEvent } from '../types/chat';
import type {
  Attachment,
  ChatState,
  MessageStatus,
  RenderBlock,
  TextBlock,
  ThinkingBlock,
  ToolBlock,
  ToolExecution,
  ToolStatus,
  WsMessage,
} from '../types/message';
import { shouldAutoApproveTool, useClientPrefsStore } from './clientPrefsStore';
import { createInitialChatState, reduceChatEvent } from './eventReducer';

export interface ConversationMeta {
  id: string;
  sessionId: string;
  title: string;
  updatedAt: string;
}

export type ConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface ChatStoreV2State extends ChatState {
  conversations: ConversationMeta[];
  activeConversationId: string | null;
  isGenerating: boolean;
  connectionStatus: ConnectionStatus;

  selectConversation: (id: string) => void;
  createConversation: () => string;
  deleteConversation: (id: string) => void;
  sendMessage: (text: string, attachments?: any[], entryNodeId?: string) => Promise<void>;
  cancelCurrentTask: () => Promise<void>;
  resetState: () => void;
  loadStartup: () => void;
}

type StoreSetter = (
  partial:
    | Partial<ChatStoreV2State>
    | ((state: ChatStoreV2State) => Partial<ChatStoreV2State>),
) => void;
type StoreGetter = () => ChatStoreV2State;

type HistoryToolResult = {
  status: 'success' | 'error';
  result?: string;
  rawInline?: string;
  rejected?: boolean;
  isAutoResult?: boolean;
};

type HistoryToolCall = {
  id?: string;
  name: string;
  arguments?: Record<string, unknown>;
};

// Why: ask is also a control tool that ends the assistant turn by requesting
// additional input. How: keep it with finish/reply/switch_node for history summaries
// and automatic result hiding. Purpose: historical ask calls do not appear as noisy
// ordinary tool executions.
const CONTROL_TOOL_NAMES = new Set(['finish', 'reply', 'switch_node', 'ask']);
const INTERNAL_USER_MESSAGE_TYPES = new Set(['tool_result', 'system', 'summary']);
const TERMINAL_TASK_EVENTS = new Set(['task_completed', 'task_cancelled', 'task_failed']);
const FINAL_RESPONSE_EVENTS = new Set(['outbound_message', ...TERMINAL_TASK_EVENTS]);

let startupLoaded = false;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let activeWsSessionId: string | null = null;
const autoApprovedApprovalIds = new Set<string>();

function clearReconnectTimer() {
  // Why: reconnect timers are module-level so they survive Zustand state updates.
  // How: every explicit stop clears the pending timeout before closing the socket.
  // Purpose: stale timers cannot reopen a WebSocket after cancel, reset, or switch.
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function stopRealtimeConnection() {
  // Why: connectSessionWS owns one browser WebSocket at a time. How: share a single
  // cleanup point with the new store's reconnect timer. Purpose: preserve the old
  // transport behavior while removing the legacy processEvent accumulator.
  clearReconnectTimer();
  activeWsSessionId = null;
  disconnectSessionWS();
}

function createConversationId(): string {
  const randomPart = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `conv-${randomPart}`;
}

function createConversationMeta(id = createConversationId(), sessionId = ''): ConversationMeta {
  const timestamp = new Date().toISOString();
  // [2026-06-01] Why: the default conversation title is visible in the sidebar.
  // How: keep the same creation flow and change only the default display text.
  // Purpose: new conversations appear localized before the first user message.
  return { id, sessionId, title: '新对话', updatedAt: timestamp };
}

function createStoreBase(): Pick<ChatStoreV2State, 'conversations' | 'activeConversationId' | 'isGenerating' | 'connectionStatus'> {
  return {
    conversations: [],
    activeConversationId: null,
    isGenerating: false,
    connectionStatus: 'idle',
  };
}

function normalizeConversationKey(value: string): string {
  if (!value) return '';
  return value.startsWith('web:') ? value.slice(4) : value;
}

function titleFromSession(conversationKey: string, sessionId: string): string {
  const normalized = normalizeConversationKey(conversationKey);
  if (normalized && normalized !== sessionId) return normalized.length > 30 ? `${normalized.slice(0, 30)}…` : normalized;
  if (conversationKey.startsWith('discord:')) return `Discord ${conversationKey.slice(8)}`;
  return sessionId ? `网页 ${sessionId.slice(0, 8)}` : '新对话';
}

function truncateTitle(text: string, limit = 30): string {
  const compact = text.replace(/\s+/g, ' ').trim();
  return compact ? Array.from(compact).slice(0, limit).join('') : '新对话';
}

function getInitialTitleFromClientPrefs(text: string, currentTitle: string | undefined): string | undefined {
  // [2026-06-01] Title behavior is now controlled by browser-local preferences.
  // Why: users asked for build-local frontend choices without backend schema changes.
  // How: first-message uses the first 50 characters, while manual and auto keep the
  // existing title until a future explicit title editor or LLM generator is wired.
  // Purpose: the current default becomes first-message without inventing a backend API.
  if (currentTitle && currentTitle !== '新对话' && currentTitle !== 'New conversation') return currentTitle;
  const mode = useClientPrefsStore.getState().titleGeneration;
  if (mode === 'first-message') return text ? truncateTitle(text, 50) : currentTitle;
  return currentTitle;
}

function upsertConversationMeta(
  conversations: readonly ConversationMeta[],
  patch: Partial<ConversationMeta> & Pick<ConversationMeta, 'id'>,
): ConversationMeta[] {
  const existing = conversations.find((conversation) => conversation.id === patch.id);
  const timestamp = patch.updatedAt || new Date().toISOString();

  if (!existing) {
    return [{ sessionId: '', title: '新对话', updatedAt: timestamp, ...patch }, ...conversations];
  }

  const updated = {
    ...existing,
    ...patch,
    sessionId: patch.sessionId !== undefined ? patch.sessionId : existing.sessionId,
    title: patch.title !== undefined ? patch.title : existing.title,
    updatedAt: timestamp,
  };

  return conversations.map((conversation) => (conversation.id === patch.id ? updated : conversation));
}

function getActiveConversation(state: ChatStoreV2State): ConversationMeta | undefined {
  return state.activeConversationId
    ? state.conversations.find((conversation) => conversation.id === state.activeConversationId)
    : undefined;
}

function shouldPreserveConversationMessagesDuringHistoryLoad(
  state: ChatStoreV2State,
  conversationId: string,
  sessionId: string,
): boolean {
  // Why: a history request can finish after the WebSocket has already delivered live
  // stream blocks for the same conversation. How: detect an active connection for the
  // target session, or the selected conversation being in a generating state. Purpose:
  // history hydration must not delete in-flight assistant output during that race.
  return activeWsSessionId === sessionId || (state.isGenerating && state.activeConversationId === conversationId);
}

function getLastEventConversationId(state: ChatState, event: SupervisorEvent, fallbackConversationId: string): string {
  const eventId = event.event_id || `${event.session_id}:${event.seq}:${event.type}`;
  const logEntry = state.eventLog.find((entry) => entry.eventId === eventId);
  return logEntry?.conversationId || fallbackConversationId;
}

function syncConversationsAfterEvent(
  conversations: readonly ConversationMeta[],
  nextChatState: ChatState,
  event: SupervisorEvent,
  fallbackConversationId: string,
): ConversationMeta[] {
  // Why: ChatState intentionally stores only normalized render data, not sidebar
  // metadata. How: after reducer replay, mirror the event's conversation/session into
  // ConversationMeta. Purpose: the new store keeps the old session list behavior while
  // message content remains fully reducer-owned.
  const conversationId = getLastEventConversationId(nextChatState, event, fallbackConversationId);
  const payload = event.payload || {};
  const isInbound = event.type === 'inbound_message';
  const existing = conversations.find((conversation) => conversation.id === conversationId);

  if (!existing && conversationId !== fallbackConversationId) {
    // Why: branch sessions and child-agent sessions can emit reducer events under their
    // own session ids. How: only allow metadata updates for conversations the sidebar
    // already knows about, or for the active fallback conversation that initiated the
    // WebSocket. Purpose: internal sessions can still hydrate reducer state without
    // polluting the user-facing conversation list.
    return [...conversations];
  }

  const inboundText = typeof payload.text === 'string' ? payload.text : '';
  const title = isInbound && inboundText && (!existing || existing.title === '新对话' || existing.title === 'New conversation')
    ? getInitialTitleFromClientPrefs(inboundText, existing?.title)
    : existing?.title;
  const sessionIdForSidebar = existing && nextChatState.conversationIdsBySession[event.session_id] !== conversationId
    ? existing.sessionId
    : event.session_id;

  return upsertConversationMeta(conversations, {
    id: conversationId,
    // Why: branch events can resolve to the parent conversation through source_inbound_seq
    // while still carrying the child session id. How: only replace the sidebar session id
    // when the reducer already maps that event session to this conversation. Purpose: the
    // visible conversation remains attached to the web-created session, not a child task.
    sessionId: sessionIdForSidebar,
    title,
    updatedAt: event.ts || new Date().toISOString(),
  });
}

function shouldStopGenerating(event: SupervisorEvent): boolean {
  return FINAL_RESPONSE_EVENTS.has(event.type);
}

function shouldCloseAfterEvent(event: SupervisorEvent): boolean {
  return TERMINAL_TASK_EVENTS.has(event.type);
}

function getToolNameForApprovalEvent(state: ChatStoreV2State, event: SupervisorEvent): string {
  const payload = event.payload || {};
  const operation = typeof payload.operation === 'string' ? payload.operation : '';
  if (operation) return operation;

  const toolCallId = typeof payload.tool_call_id === 'string' ? payload.tool_call_id : '';
  if (!toolCallId) return '';

  // [2026-06-01] Why: some approval events identify only the tool call id. How:
  // look up the normalized ToolExecution that the reducer just updated. Purpose:
  // auto-approval can still use the same clientPrefs tool-name rules.
  const stableId = state.toolStableIdByExternalId[toolCallId];
  return stableId ? state.toolExecutionsById[stableId]?.name || '' : '';
}

function maybeAutoApproveApprovalRequest(event: SupervisorEvent, get: StoreGetter) {
  if (event.type !== 'approval_requested') return;
  const payload = event.payload || {};
  const approvalId = typeof payload.approval_id === 'string' ? payload.approval_id : '';
  if (!approvalId || autoApprovedApprovalIds.has(approvalId)) return;

  const state = get();
  const toolName = getToolNameForApprovalEvent(state, event);
  const prefs = useClientPrefsStore.getState();
  if (!toolName || !shouldAutoApproveTool(toolName, prefs.autoApproveTools)) return;

  autoApprovedApprovalIds.add(approvalId);
  // [2026-06-01] Why: frontend auto-approval should behave like a local click, not
  // a backend policy change. How: submit the normal approval decision endpoint and
  // ignore transport errors so the pending card remains available for manual action.
  // Purpose: low-risk local rules can proceed while high-risk tools still need users.
  void decideApproval(approvalId, 'allow', 'auto-approved by client preference').catch(() => {
    autoApprovedApprovalIds.delete(approvalId);
  });
}

function startSessionWebSocket(sessionId: string, conversationId: string, set: StoreSetter, get: StoreGetter) {
  clearReconnectTimer();
  activeWsSessionId = sessionId;
  set({ connectionStatus: 'connecting' });

  const currentSeq = get().lastSeqBySession[sessionId] || 0;
  connectSessionWS(
    sessionId,
    currentSeq,
    (event) => {
      set((state) => {
        // Why: WebSocket delivery and catch-up use the same SupervisorEvent shape.
        // How: pass the whole current Zustand state to the pure reducer, then merge
        // the returned ChatState fields back into the store. Purpose: eliminate the
        // legacy processEvent path and all pending stream/tool accumulators.
        const reducedState = reduceChatEvent(state, event);
        return {
          ...reducedState,
          conversations: syncConversationsAfterEvent(state.conversations, reducedState, event, conversationId),
          connectionStatus: 'open',
          isGenerating: shouldStopGenerating(event) ? false : state.isGenerating,
        };
      });

      maybeAutoApproveApprovalRequest(event, get);

      if (shouldCloseAfterEvent(event)) {
        stopRealtimeConnection();
        // [2026-06-01] Terminal task events intentionally close realtime transport.
        // Why: task completion is a normal end state, but using `closed` made the
        // right panel show a red disconnected warning after successful replies. How:
        // return to idle after local cleanup. Purpose: reserve `closed` for
        // unexpected disconnects while generation is not actively closing itself.
        set({ connectionStatus: 'idle', isGenerating: false });
      }
    },
    () => {
      if (activeWsSessionId !== sessionId) return;
      // [2026-06-01] Mark the socket healthy as soon as the browser opens it.
      // Why: relying on the first non-ping Supervisor event delayed the visible
      // transition to open and caused stale status in quiet periods. How: the API
      // client now exposes an onOpen callback that updates this session only if it
      // is still current. Purpose: keep the right panel aligned with transport state.
      set({ connectionStatus: 'open' });
    },
    () => {
      if (activeWsSessionId !== sessionId) return;
      if (get().isGenerating) {
        set({ connectionStatus: 'reconnecting' });
        reconnectTimer = setTimeout(() => startSessionWebSocket(sessionId, conversationId, set, get), 2000);
        return;
      }
      activeWsSessionId = null;
      set({ connectionStatus: 'closed' });
    },
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringifyContent(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function collapseForPreview(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function truncateForPreview(value: string, limit: number): string {
  const chars = Array.from(value);
  return chars.length > limit ? `${chars.slice(0, limit).join('')}…` : value;
}

function summarizeArguments(toolName: string, args: Record<string, unknown> | undefined): string {
  if (!args || Object.keys(args).length === 0) return '';
  if (CONTROL_TOOL_NAMES.has(toolName) && typeof args.text === 'string') return '';

  return Object.entries(args)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${truncateForPreview(collapseForPreview(stringifyContent(value)), 80)}`)
    .join(', ');
}

function buildHistoryToolResultIndex(messages: readonly StructuredMessage[]): Map<string, HistoryToolResult> {
  const resultIndex = new Map<string, HistoryToolResult>();

  for (const message of messages) {
    if (message.role !== 'tool' || !message.tool_call_id) continue;

    const toolName = message.tool_name || message.name || '';
    const rawContent = stringifyContent(message.content);
    const trimmed = rawContent.trim();
    // [2026-06-01] Why: old tool result history can start with a cross-mark emoji,
    // but the migrated frontend should not generate emoji-prefixed results. How:
    // preserve legacy detection while accepting plain rejected text. Purpose: Material
    // Symbols migration remains compatible with existing stored conversations.
    const rejected = /^\s*(?:❌\s*)?REJECTED(?:\b|:)/i.test(trimmed);
    const status: HistoryToolResult['status'] = rejected || /^ERROR(?:\b|:)/i.test(trimmed) ? 'error' : 'success';
    const isAutoResult = status === 'success' && CONTROL_TOOL_NAMES.has(toolName) && trimmed.toLowerCase() === 'ok';

    resultIndex.set(message.tool_call_id, {
      status,
      rawInline: rawContent,
      rejected: rejected || undefined,
      isAutoResult: isAutoResult || undefined,
      result: isAutoResult
        ? undefined
        : status === 'error'
          ? rawContent
          : truncateForPreview(collapseForPreview(rawContent), 120) || undefined,
    });
  }

  return resultIndex;
}

function normalizeHistoryToolCalls(value: StructuredMessage['tool_calls']): HistoryToolCall[] {
  if (!Array.isArray(value)) return [];

  return value.map((toolCall, index) => ({
    id: toolCall.id || `history-tool-${index}`,
    name: toolCall.name || 'unknown',
    arguments: isRecord(toolCall.arguments) ? toolCall.arguments : undefined,
  }));
}

function extractControlToolText(toolCalls: readonly HistoryToolCall[]): { text: string; toolName?: string } {
  for (const toolCall of toolCalls) {
    if (!CONTROL_TOOL_NAMES.has(toolCall.name)) continue;
    const text = typeof toolCall.arguments?.text === 'string' ? toolCall.arguments.text : '';
    if (text) return { text, toolName: toolCall.name };
  }
  return { text: '' };
}

function historyToolStatus(result: HistoryToolResult | undefined): ToolStatus {
  if (!result) return 'success';
  return result.status;
}

function createHistoryToolExecutions(
  toolCalls: readonly HistoryToolCall[],
  resultIndex: Map<string, HistoryToolResult>,
  messageId: string,
  createdAt: string,
  sessionId: string,
  nodeId?: string,
): ToolExecution[] {
  return toolCalls.map((toolCall, index) => {
    const result = toolCall.id ? resultIndex.get(toolCall.id) : undefined;
    const stableId = `${messageId}|history-tool:${toolCall.id || index}`;
    return {
      stableId,
      messageId,
      id: toolCall.id,
      index,
      name: toolCall.name,
      status: historyToolStatus(result),
      arguments: toolCall.arguments,
      argumentsText: toolCall.arguments ? JSON.stringify(toolCall.arguments) : undefined,
      summary: summarizeArguments(toolCall.name, toolCall.arguments),
      result: result?.result,
      rawInline: result?.rawInline,
      rejected: result?.rejected,
      hidden: CONTROL_TOOL_NAMES.has(toolCall.name) && result?.isAutoResult,
      nodeId,
      createdAt,
      updatedAt: createdAt,
      eventIds: [`history:${sessionId}:${messageId}:tool:${index}`],
    } satisfies ToolExecution;
  });
}

function createTextBlock(id: string, createdAt: string, text: string): TextBlock {
  return {
    id,
    kind: 'text',
    text,
    delivery: 'history',
    streaming: false,
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

function createThinkingBlock(
  id: string,
  createdAt: string,
  text: string,
  startedAt?: string,
  endedAt?: string,
): ThinkingBlock {
  return {
    id,
    kind: 'thinking',
    text,
    streaming: false,
    startedAt,
    endedAt,
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

function createToolBlock(id: string, createdAt: string, toolIds: readonly string[]): ToolBlock {
  return {
    id,
    kind: 'tool',
    toolIds: [...toolIds],
    createdAt,
    updatedAt: createdAt,
    eventIds: [`${id}:history`],
  };
}

function appendHistoryMessage(state: ChatState, message: WsMessage, tools: readonly ToolExecution[]): ChatState {
  const order = state.messageOrderByConversation[message.conversationId] || [];
  const nextToolExecutionsById = { ...state.toolExecutionsById };
  const nextToolExecutionOrder = [...state.toolExecutionOrder];

  for (const tool of tools) {
    nextToolExecutionsById[tool.stableId] = tool;
    if (!nextToolExecutionOrder.includes(tool.stableId)) nextToolExecutionOrder.push(tool.stableId);
  }

  return {
    ...state,
    messagesById: { ...state.messagesById, [message.id]: message },
    messageOrderByConversation: {
      ...state.messageOrderByConversation,
      [message.conversationId]: order.includes(message.id) ? order : [...order, message.id],
    },
    toolExecutionsById: nextToolExecutionsById,
    toolExecutionOrder: nextToolExecutionOrder,
  };
}

function removeConversationMessages(state: ChatState, conversationId: string): ChatState {
  const removedMessageIds = new Set(state.messageOrderByConversation[conversationId] || []);
  if (removedMessageIds.size === 0) return state;

  const messagesById = { ...state.messagesById };
  for (const messageId of removedMessageIds) delete messagesById[messageId];

  const messageOrderByConversation = { ...state.messageOrderByConversation };
  delete messageOrderByConversation[conversationId];

  const toolExecutionsById = { ...state.toolExecutionsById };
  const toolExecutionOrder = state.toolExecutionOrder.filter((toolId) => {
    const tool = state.toolExecutionsById[toolId];
    if (tool && removedMessageIds.has(tool.messageId)) {
      delete toolExecutionsById[toolId];
      return false;
    }
    return true;
  });

  return { ...state, messagesById, messageOrderByConversation, toolExecutionsById, toolExecutionOrder };
}

function hydrateStructuredHistory(
  state: ChatState,
  sessionId: string,
  conversationId: string,
  history: readonly StructuredMessage[],
  preserveExistingMessages = false,
): ChatState {
  // Why: /history returns persisted ConversationStore messages, not SupervisorEvent
  // records. How: convert that structured history into the same WsMessage blocks and
  // ToolExecution tables used by the reducer. Purpose: cold startup can show existing
  // sessions while live WebSocket events continue to use reduceChatEvent only.
  const resultIndex = buildHistoryToolResultIndex(history);
  // Why: normal cold-start hydration should replace stale history, but an active
  // generation may already have live WebSocket messages in this conversation. How:
  // the caller can skip the removal step for active sessions. Purpose: late history
  // responses do not erase in-flight stream or tool blocks.
  let nextState = preserveExistingMessages ? state : removeConversationMessages(state, conversationId);
  let accumulatedThinking: string[] = [];
  let accumulatedTools: ToolExecution[] = [];
  let accumulatedCreatedAt = '';

  const resetAccumulatedAssistant = () => {
    accumulatedThinking = [];
    accumulatedTools = [];
    accumulatedCreatedAt = '';
  };

  const pushAssistantMessage = (
    sourceMessage: StructuredMessage,
    text: string,
    currentTools: readonly ToolExecution[],
    completionType?: WsMessage['completionType'],
  ) => {
    const createdAt = sourceMessage.created_at || accumulatedCreatedAt || new Date().toISOString();
    const messageId = `message:${conversationId}:history:${sourceMessage.id || nextState.messageOrderByConversation[conversationId]?.length || 0}`;
    const thinkingText = [...accumulatedThinking, ...(sourceMessage.thinking ? [sourceMessage.thinking] : [])]
      .filter((item) => item.trim())
      .join('\n---\n');
    const tools = [...accumulatedTools, ...currentTools].map((tool) => ({
      ...tool,
      // Why: a tool-only assistant history entry may be merged into a later visible
      // assistant message. How: keep the stable tool id from its source entry but
      // update messageId to the visible message that owns the rendered tool block.
      // Purpose: avoid duplicate ids while preserving selector lookups for tools.
      messageId,
    }));
    const blocks: RenderBlock[] = [];

    if (thinkingText) {
      blocks.push(createThinkingBlock(
        `${messageId}|block:thinking:history`,
        createdAt,
        thinkingText,
        sourceMessage.reasoning_started_at,
        sourceMessage.reasoning_ended_at,
      ));
    }
    if (tools.length > 0) {
      const blockId = `${messageId}|block:tool:history`;
      blocks.push(createToolBlock(blockId, createdAt, tools.map((tool) => tool.stableId)));
      tools.forEach((tool, index) => { tools[index] = { ...tool, blockId }; });
    }
    if (text) blocks.push(createTextBlock(`${messageId}|block:text:history`, createdAt, text));

    const status: MessageStatus = 'completed';
    const message: WsMessage = {
      id: messageId,
      conversationId,
      sessionId,
      role: 'assistant',
      status,
      createdAt,
      updatedAt: createdAt,
      source: { nodeId: sourceMessage.source_node_id || undefined },
      blocks,
      eventIds: [`history:${sessionId}:${sourceMessage.id || messageId}`],
      hydratedFromHistory: true,
      ...(completionType && { completionType }),
    };

    nextState = appendHistoryMessage(nextState, message, tools.map((tool) => ({
      ...tool,
      eventIds: [`history:${sessionId}:${messageId}:tool:${tool.index ?? 0}`],
    })));
    resetAccumulatedAssistant();
  };

  const flushDanglingAssistant = () => {
    if (accumulatedThinking.length === 0 && accumulatedTools.length === 0) return;
    // Why: ConversationStore can end or switch turns after a tool-only assistant
    // entry. How: emit an empty assistant message carrying the accumulated thinking
    // and tool blocks before processing the next user row. Purpose: preserve history
    // that would otherwise disappear during cold startup hydration.
    pushAssistantMessage({
      id: `dangling-${nextState.messageOrderByConversation[conversationId]?.length || 0}`,
      role: 'assistant',
      content: '',
      created_at: accumulatedCreatedAt || new Date().toISOString(),
    }, '', []);
  };

  for (const message of history) {
    if (message.role === 'user') {
      flushDanglingAssistant();
      if (INTERNAL_USER_MESSAGE_TYPES.has(message.message_type || '')) continue;

      const createdAt = message.created_at || new Date().toISOString();
      const messageId = `message:${conversationId}:history:${message.id || `user-${nextState.messageOrderByConversation[conversationId]?.length || 0}`}`;
      const text = stringifyContent(message.content);
      const wsMessage: WsMessage = {
        id: messageId,
        conversationId,
        sessionId,
        role: 'user',
        status: 'completed',
        createdAt,
        updatedAt: createdAt,
        source: { nodeId: message.source_node_id || undefined },
        blocks: [createTextBlock(`${messageId}|block:text:history`, createdAt, text)],
        eventIds: [`history:${sessionId}:${message.id || messageId}`],
        hydratedFromHistory: true,
      };
      nextState = appendHistoryMessage(nextState, wsMessage, []);
      continue;
    }

    if (message.role !== 'assistant') continue;

    const pendingMessageId = `message:${conversationId}|history-pending:${message.id || 'assistant'}`;
    const toolCalls = normalizeHistoryToolCalls(message.tool_calls);
    const currentTools = createHistoryToolExecutions(toolCalls, resultIndex, pendingMessageId, message.created_at || new Date().toISOString(), sessionId, message.source_node_id);
    const controlText = extractControlToolText(toolCalls);
    const contentText = stringifyContent(message.content);
    const displayText = contentText.trim() ? contentText : controlText.text;
    const hasControlTool = toolCalls.some((toolCall) => CONTROL_TOOL_NAMES.has(toolCall.name));

    if (hasControlTool && controlText.text) {
      // Why: finish/reply/ask/switch_node are persisted as standalone assistant tool
      // calls by the backend control-tool guard. How: flush any accumulated tool-only
      // assistant history before rendering the visible control text. Purpose: final
      // control-tool output cannot merge into the previous tool card during hydration.
      flushDanglingAssistant();
      // Determine completionType from the control tool name
      const ctName = controlText.toolName;
      const ctType: WsMessage['completionType'] =
        ctName === 'finish' ? 'finish' : ctName === 'ask' ? 'ask' : ctName === 'reply' ? 'reply' : undefined;
      pushAssistantMessage(message, displayText, currentTools, ctType);
      continue;
    }

    if (!displayText && !hasControlTool) {
      if (message.thinking) accumulatedThinking.push(message.thinking);
      if (currentTools.length > 0) accumulatedTools.push(...currentTools);
      if (!accumulatedCreatedAt) accumulatedCreatedAt = message.created_at || '';
      continue;
    }

    pushAssistantMessage(message, displayText, currentTools);
  }

  flushDanglingAssistant();
  return {
    ...nextState,
    conversationIdsBySession: {
      ...nextState.conversationIdsBySession,
      [sessionId]: conversationId,
    },
  };
}

async function loadSessionHistoryIntoStore(conversationId: string, sessionId: string, set: StoreSetter, get: StoreGetter) {
  let history: StructuredMessage[] = [];
  let events: SupervisorEvent[] = [];

  try {
    [history, events] = await Promise.all([
      getSessionHistory(sessionId, 200),
      pollEvents(sessionId, 0),
    ]);
  } catch {
    return;
  }

  set((state) => {
    const preserveExistingMessages = shouldPreserveConversationMessagesDuringHistoryLoad(state, conversationId, sessionId);
    const hydrated = history.length > 0
      ? hydrateStructuredHistory(state, sessionId, conversationId, history, preserveExistingMessages)
      : state;
    const maxSeq = events.reduce((max, event) => Math.max(max, event.seq || 0), hydrated.lastSeqBySession[sessionId] || 0);
    return {
      ...hydrated,
      lastSeqBySession: { ...hydrated.lastSeqBySession, [sessionId]: maxSeq },
      conversations: upsertConversationMeta(state.conversations, {
        id: conversationId,
        sessionId,
        updatedAt: new Date().toISOString(),
      }),
    };
  });
}

async function loadStartupSessions(set: StoreSetter, get: StoreGetter) {
  if (startupLoaded) return;
  startupLoaded = true;

  const serverSessions = await listSessions('web', 50);
  if (!serverSessions || serverSessions.length === 0) return;

  const conversations = serverSessions.map((session): ConversationMeta => {
    const conversationId = normalizeConversationKey(session.conversation_key) || session.session_id;
    return {
      id: conversationId,
      sessionId: session.session_id,
      title: titleFromSession(session.conversation_key, session.session_id),
      updatedAt: session.updated_at || session.created_at || new Date().toISOString(),
    };
  });

  set((state) => ({
    conversations,
    activeConversationId: state.activeConversationId || conversations[0]?.id || null,
    conversationIdsBySession: conversations.reduce<Record<string, string>>((acc, conversation) => {
      if (conversation.sessionId) acc[conversation.sessionId] = conversation.id;
      return acc;
    }, { ...state.conversationIdsBySession }),
  }));

  const first = conversations[0];
  if (first?.sessionId) {
    await loadSessionHistoryIntoStore(first.id, first.sessionId, set, get);
  }
}

export const useChatStoreV2 = create<ChatStoreV2State>((set, get) => ({
  ...createInitialChatState(),
  ...createStoreBase(),

  resetState: () => {
    stopRealtimeConnection();
    startupLoaded = false;
    autoApprovedApprovalIds.clear();
    set({
      ...createInitialChatState(),
      ...createStoreBase(),
    });
  },

  selectConversation: (id) => {
    const target = get().conversations.find((conversation) => conversation.id === id);
    set({ activeConversationId: id });
    if (target?.sessionId) {
      void loadSessionHistoryIntoStore(target.id, target.sessionId, set, get);
    }
  },

  createConversation: () => {
    const conversation = createConversationMeta();
    set((state) => ({
      conversations: [conversation, ...state.conversations],
      activeConversationId: conversation.id,
    }));
    return conversation.id;
  },

  deleteConversation: (id) => {
    const conversation = get().conversations.find((item) => item.id === id);
    if (conversation?.sessionId) {
      void deleteSession(conversation.sessionId).catch(() => undefined);
    }

    set((state) => {
      const conversations = state.conversations.filter((item) => item.id !== id);
      const activeConversationId = state.activeConversationId === id ? conversations[0]?.id || null : state.activeConversationId;
      const nextChatState = removeConversationMessages(state, id);
      const conversationIdsBySession = { ...nextChatState.conversationIdsBySession };
      if (conversation?.sessionId) delete conversationIdsBySession[conversation.sessionId];

      return {
        ...nextChatState,
        conversationIdsBySession,
        conversations,
        activeConversationId,
      };
    });
  },

  sendMessage: async (text, attachments, entryNodeId) => {
    const trimmed = text.trim();
    if (!trimmed && (!attachments || attachments.length === 0)) return;

    const state = get();
    const conversationId = state.activeConversationId || state.createConversation();
    const conversationKey = `web:${conversationId}`;
    const existingConversation = get().conversations.find((conversation) => conversation.id === conversationId);

    set((current) => ({
      conversations: upsertConversationMeta(current.conversations, {
        id: conversationId,
        sessionId: existingConversation?.sessionId || '',
        title: (existingConversation?.title === '新对话' || existingConversation?.title === 'New conversation') && trimmed
          ? getInitialTitleFromClientPrefs(trimmed, existingConversation?.title)
          : existingConversation?.title,
        updatedAt: new Date().toISOString(),
      }),
      activeConversationId: conversationId,
      isGenerating: true,
      connectionStatus: 'connecting',
    }));

    let sessionId = '';
    try {
      // Upload files that have a raw File object, then collect server paths
      let uploadedAttachments: { name: string; size: number; type?: string; path?: string; mime_type?: string }[] | undefined;
      if (attachments && attachments.length > 0) {
        const uploaded = await Promise.all(
          attachments.map(async (attachment: Attachment) => {
            if (attachment.file) {
              const result = await uploadAttachment(attachment.file, conversationKey);
              return {
                name: result.name,
                size: result.size,
                type: result.type,
                path: result.path,
                mime_type: result.mime_type,
              };
            }
            return {
              name: attachment.name,
              size: attachment.size ?? 0,
              type: attachment.type,
              path: attachment.path,
              mime_type: attachment.mime_type,
            };
          }),
        );
        uploadedAttachments = uploaded;
      }

      const result = await postInbound({
        conversation_key: conversationKey,
        text: trimmed,
        attachments: uploadedAttachments,
        use_context: true,
        entry_node_id: entryNodeId,
      });
      sessionId = result.session_id;
    } catch (error) {
      set({ isGenerating: false, connectionStatus: 'closed' });
      return;
    }

    set((current) => ({
      conversations: upsertConversationMeta(current.conversations, {
        id: conversationId,
        sessionId,
        updatedAt: new Date().toISOString(),
      }),
      conversationIdsBySession: {
        ...current.conversationIdsBySession,
        [sessionId]: conversationId,
      },
      lastSeqBySession: {
        ...current.lastSeqBySession,
        [sessionId]: current.lastSeqBySession[sessionId] || 0,
      },
    }));

    stopRealtimeConnection();
    startSessionWebSocket(sessionId, conversationId, set, get);
  },

  cancelCurrentTask: async () => {
    const activeConversation = getActiveConversation(get());
    if (!activeConversation?.sessionId) return;

    try {
      await cancelActiveTasks(activeConversation.sessionId);
    } catch {
      // Why: cancel is a best-effort transport action. How: ignore API failures here
      // and still clear the local realtime state. Purpose: the composer should not
      // remain locked if the cancel endpoint races with task completion.
    }

    stopRealtimeConnection();
    // [2026-06-01] Cancellation is a deliberate local stop, not a connection error.
    // Why: showing Disconnected after the user cancels a task suggests a transport
    // failure even though the socket was closed by design. How: clear generation and
    // return to idle. Purpose: only unexpected disconnects stay red in the panel.
    set({ isGenerating: false, connectionStatus: 'idle' });
  },

  loadStartup: () => {
    void loadStartupSessions(set, get);
  },
}));
