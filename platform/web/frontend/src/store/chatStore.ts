// [2026-05-16] Rewritten: real Supervisor API, event polling, stream preview, localStorage persistence.
import { create } from 'zustand';

// [2026-05-17] Phase 3 imports the public WebSocket controls from the API barrel.
// The remaining Supervisor helpers stay on the concrete client module to avoid
// widening the barrel export surface beyond the realtime change requested here.
import { connectSessionWS, disconnectSessionWS } from '../api';
import { postInbound, cancelActiveTasks, getSessionHistory, listSessions, deleteSession } from '../api/supervisorClient';
import type { StructuredMessage } from '../api/supervisorClient';
import type { Attachment, ChatMessage, Conversation, StreamPreviewState, SupervisorEvent, ToolCall } from '../types';
import { createEmptyConversation, createMessageId } from './mockData';

// [2026-05-17] localStorage session cache removed — backend is single source of truth.

// ── Persistence helpers ──
// [2026-05-17] All session/conversation data comes exclusively from the backend.
// localStorage is no longer used for sessionMap or conversations. This prevents
// deleted sessions from "reviving" after page refresh.
function saveConversations(_convs: Conversation[]) { /* no-op */ }
function saveSessionMap(_map: Record<string, string>) { /* no-op */ }

// ── Stream preview initial state ──

const emptyPreview: StreamPreviewState = {
  thinkingPreview: '',
  textPreview: '',
  progressLines: [],
  retryInfo: '',
  thinkingStartTime: null,
  isActive: false,
};

// ── Store types ──

interface ChatState {
  conversations: Conversation[];
  activeConversationId: string | null;
  activeConversation: Conversation | null;
  typingConversationId: string | null;

  // Session tracking
  sessionMap: Record<string, string>; // conversationId → sessionId
  lastEventSeq: Record<string, number>; // sessionId → lastSeq
  currentTaskId: string | null;
  isGenerating: boolean;

  // Stream preview
  streamPreview: StreamPreviewState;

  // Actions
  selectConversation: (conversationId: string) => void;
  createConversation: () => string;
  deleteConversation: (conversationId: string) => void;
  sendActiveMessage: (text: string, attachments?: Attachment[], entryNodeId?: string) => Promise<void>;
  cancelCurrentTask: () => Promise<void>;
  addMessage: (conversationId: string, message: ChatMessage) => void;
  updateConversationTitle: (conversationId: string, title: string) => void;
  resetState: () => void;
  _triggerStartupLoad: () => void;
}

// ── Helpers ──

const findActive = (convs: Conversation[], id: string | null): Conversation | null => {
  if (!id) return convs[0] ?? null;
  return convs.find(c => c.id === id) ?? convs[0] ?? null;
};

const updateMessages = (
  convs: Conversation[],
  convId: string,
  updater: (msgs: ChatMessage[]) => ChatMessage[],
): Conversation[] =>
  convs.map(c => c.id !== convId ? c : { ...c, updatedAt: new Date().toISOString(), messages: updater(c.messages) });

// ── Polling state (module-level to avoid closure leaks) ──

let _pollTimer: ReturnType<typeof setInterval> | null = null;
let _pollSlowTimer: ReturnType<typeof setTimeout> | null = null;

function stopPolling() {
  // [2026-05-17] This function is now the single stop point for all event
  // listeners. It preserves old polling cleanup and also closes the Phase 3
  // WebSocket so cancel, completion, reset, and conversation changes share cleanup.
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  if (_pollSlowTimer) { clearTimeout(_pollSlowTimer); _pollSlowTimer = null; }
  disconnectSessionWS();
}

// ── Store ──

const initialConvs: Conversation[] = [];
const initialSessionMap: Record<string, string> = {};

// Load sessions from backend and rebuild conversations on startup
let _startupLoaded = false;
async function _startupLoadSessions(set: any, get: any) {
  if (_startupLoaded) return;
  _startupLoaded = true;

  const serverSessions = await listSessions('web', 50);
  if (!serverSessions || serverSessions.length === 0) return;

  // Build conversations from server sessions
  const newConvs: Conversation[] = [];
  const newSessionMap: Record<string, string> = {};

  for (const ss of serverSessions) {
    // Use conversation_key as convId, strip common prefixes for display
    const convId = ss.session_id;
    newSessionMap[convId] = ss.session_id;
    // Build a friendly title from conversation_key
    let title = ss.conversation_key || ss.session_id.slice(0, 8);
    if (title.startsWith('web:')) title = `Web ${title.slice(4, 12)}`;
    else if (title.startsWith('discord:')) title = `Discord ${title.slice(8)}`;
    else if (title.length > 24) title = title.slice(0, 24) + '…';

    newConvs.push({
      id: convId,
      sessionId: ss.session_id,
      title,
      messages: [],
      updatedAt: ss.updated_at || ss.created_at || new Date().toISOString(),
    });
  }

  // [2026-05-17] No merge with localStorage — backend is the single source of truth.
  set(() => ({
    conversations: newConvs,
    sessionMap: newSessionMap,
    activeConversationId: newConvs[0]?.id ?? null,
    activeConversation: newConvs[0] ?? null,
  }));

  // Load history for the first (most recent) conversation
  if (newConvs.length > 0 && newSessionMap[newConvs[0].id]) {
    loadSessionHistory(newConvs[0].id, newSessionMap[newConvs[0].id], set, get);
  }
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: initialConvs,
  activeConversationId: initialConvs[0]?.id ?? null,
  activeConversation: initialConvs[0] ?? null,
  typingConversationId: null,
  sessionMap: initialSessionMap,
  lastEventSeq: {},
  currentTaskId: null,
  isGenerating: false,
  streamPreview: { ...emptyPreview },

  resetState: () => {
    stopPolling();
    set({
      conversations: [],
      activeConversationId: null,
      activeConversation: null,
      typingConversationId: null,
      sessionMap: {},
      lastEventSeq: {},
      currentTaskId: null,
      isGenerating: false,
      streamPreview: { ...emptyPreview },
    });
    saveSessionMap({});
  },

  selectConversation: (id) => {
    set(s => ({ activeConversationId: id, activeConversation: findActive(s.conversations, id) }));
    // Load history from Supervisor if we have a session mapping
    const sessionId = get().sessionMap[id];
    if (sessionId) {
      loadSessionHistory(id, sessionId, set, get);
    }
  },

  // Called once after first render to load history for initial conversation
  _triggerStartupLoad: () => _startupLoadSessions(set, get),

  createConversation: () => {
    const conv = createEmptyConversation();
    set(s => {
      const convs = [conv, ...s.conversations];
      saveConversations(convs);
      return { conversations: convs, activeConversationId: conv.id, activeConversation: conv, typingConversationId: null };
    });
    return conv.id;
  },

  deleteConversation: (id) => {
    // Delete from backend
    const sessionId = get().sessionMap[id];
    if (sessionId) {
      deleteSession(sessionId).catch(() => {});
    }
    set(s => {
      const convs = s.conversations.filter(c => c.id !== id);
      const newMap = { ...s.sessionMap };
      delete newMap[id];
      saveSessionMap(newMap);
      const newActive = s.activeConversationId === id ? (convs[0]?.id ?? null) : s.activeConversationId;
      return {
        conversations: convs,
        sessionMap: newMap,
        activeConversationId: newActive,
        activeConversation: findActive(convs, newActive),
      };
    });
  },

  addMessage: (conversationId, message) => {
    set(s => {
      const convs = updateMessages(s.conversations, conversationId, msgs => [...msgs, message]);
      saveConversations(convs);
      return { conversations: convs, activeConversation: findActive(convs, s.activeConversationId) };
    });
  },

  updateConversationTitle: (conversationId, title) => {
    set(s => {
      const convs = s.conversations.map(c => c.id !== conversationId ? c : { ...c, title });
      saveConversations(convs);
      return { conversations: convs, activeConversation: findActive(convs, s.activeConversationId) };
    });
  },

  cancelCurrentTask: async () => {
    const { sessionMap, activeConversationId } = get();
    if (!activeConversationId) return;
    const sessionId = sessionMap[activeConversationId];
    if (!sessionId) return;
    try {
      await cancelActiveTasks(sessionId);
    } catch { /* ignore */ }
    stopPolling();
    set({ isGenerating: false, typingConversationId: null, streamPreview: { ...emptyPreview } });
  },

  sendActiveMessage: async (text, attachments, entryNodeId) => {
    const trimmed = text.trim();
    if (!trimmed && (!attachments || attachments.length === 0)) return;

    const state = get();
    const conversationId = state.activeConversationId ?? state.createConversation();
    const conversationKey = `web:${conversationId}`;

    // Append user message
    const userMsg: ChatMessage = {
      id: createMessageId('user'),
      conversationId,
      role: 'user',
      content: trimmed,
      createdAt: new Date().toISOString(),
      attachments,
    };

    set(s => {
      const convs = updateMessages(s.conversations, conversationId, msgs => [...msgs, userMsg]);
      // Update title if first user message
      const conv = convs.find(c => c.id === conversationId);
      if (conv && conv.messages.filter(m => m.role === 'user').length === 1) {
        // [2026-06-01] Why: this legacy store can still seed visible sidebar
        // titles in fallback paths. How: localize only the empty default while
        // keeping first-message title behavior unchanged. Purpose: no English
        // fallback appears when a blank attachment-only conversation is created.
        conv.title = trimmed.slice(0, 30) || '新对话';
      }
      saveConversations(convs);
      return {
        conversations: convs,
        activeConversation: findActive(convs, conversationId),
        activeConversationId: conversationId,
        typingConversationId: conversationId,
        isGenerating: true,
        streamPreview: { ...emptyPreview, isActive: true, thinkingStartTime: Date.now() },
      };
    });

    // POST inbound
    let sessionId: string;
    const existingSessionId = get().sessionMap[conversationId];
    try {
      const result = await postInbound({
        conversation_key: conversationKey,
        text: trimmed,
        attachments: attachments?.map(a => ({ name: a.name, size: a.size, type: a.type })),
        use_context: true,
        // Always pass entry_node_id. Backend priority: session_override (AI switch) > this > default.
        // Not passing it causes fallback to runtime.yaml default (ereuna_main) which is wrong for web.
        entry_node_id: entryNodeId,
      });
      sessionId = result.session_id;

      // Save session mapping (don't reset seq for existing sessions)
      set(s => {
        const newMap = { ...s.sessionMap, [conversationId]: sessionId };
        saveSessionMap(newMap);
        const seqUpdate = existingSessionId ? s.lastEventSeq : { ...s.lastEventSeq, [sessionId]: 0 };
        return { sessionMap: newMap, lastEventSeq: seqUpdate };
      });
    } catch (err) {
      const errorMsg: ChatMessage = {
        id: createMessageId('error'),
        conversationId,
        role: 'system',
        // [2026-06-01] Why: the icon migration removes emoji glyphs from user-visible
        // frontend strings as well as JSX controls. How: keep the same error message
        // without a warning emoji prefix. Purpose: grep-based checks can confirm no
        // old icon glyphs remain in the source.
        content: `无法连接调度器：${err instanceof Error ? err.message : String(err)}`,
        createdAt: new Date().toISOString(),
      };
      set(s => {
        const convs = updateMessages(s.conversations, conversationId, msgs => [...msgs, errorMsg]);
        saveConversations(convs);
        return {
          conversations: convs,
          activeConversation: findActive(convs, conversationId),
          typingConversationId: null,
          isGenerating: false,
          streamPreview: { ...emptyPreview },
        };
      });
      return;
    }

    // [2026-05-17] Phase 3 starts a WebSocket listener instead of the active
    // polling loop. The old polling state is still cleaned up first so fallback
    // code elsewhere can coexist without duplicate event delivery.
    stopPolling();

    const startWS = () => {
      // [2026-05-17] A reconnect timer may fire after the user cancels or the
      // task finishes. Guard here as well as in onDisconnect so stale timers cannot
      // reopen a WebSocket for an inactive generation.
      if (!get().isGenerating) return;
      const currentSeq = get().lastEventSeq[sessionId] || 0;
      connectSessionWS(
        sessionId,
        currentSeq,
        (ev) => {
          // [2026-05-17] The WebSocket event shape is the same SupervisorEvent
          // used by polling, so processEvent remains the single reducer for both
          // transport paths while the store records the newest sequence after each event.
          const evSeq = ev.seq || 0;
          processEvent(ev, conversationId, set, get);
          if (evSeq > (get().lastEventSeq[sessionId] || 0)) {
            set(s => ({ lastEventSeq: { ...s.lastEventSeq, [sessionId]: evSeq } }));
          }
        },
        undefined,
        () => {
          // [2026-06-01] Keep legacy store disconnect handling in the disconnect slot.
          // Why: connectSessionWS now accepts an onOpen callback before onDisconnect.
          // How: pass undefined for onOpen because this legacy store does not expose
          // connectionStatus. Purpose: avoid treating disconnect logic as an open
          // callback after the API signature change.
          if (get().isGenerating) {
            setTimeout(startWS, 2000);
          }
        },
      );
    };
    startWS();
  },
}));

// ── Turn accumulator (module-level, reset per outbound) ──

let _pendingThinking = '';
let _pendingTools: ToolCall[] = [];

function resetTurnAccumulator() {
  _pendingThinking = '';
  _pendingTools = [];
}

// ── Event processor ──

function processEvent(
  ev: SupervisorEvent,
  conversationId: string,
  set: (fn: (s: ChatState) => Partial<ChatState>) => void,
  get: () => ChatState,
) {
  const p = ev.payload;

  switch (ev.type) {
    case 'stream_delta': {
      const content = (p.content as string) || '';
      const sdType = (p.type as string) || 'text';
      if (!content) break;
      // Accumulate full thinking chain (not just last 300 chars)
      if (sdType === 'thinking') {
        _pendingThinking += content;
      }
      // Update live preview
      set(s => {
        const sp = { ...s.streamPreview };
        if (sdType === 'thinking') {
          sp.thinkingPreview = _pendingThinking.slice(-300);
          if (!sp.thinkingStartTime) sp.thinkingStartTime = Date.now();
        } else if (sdType === 'text') {
          if (sp.thinkingPreview) {
            sp.thinkingPreview = '';
            sp.thinkingStartTime = Date.now();
          }
          sp.textPreview = (sp.textPreview + content).slice(-500);
        }
        sp.isActive = true;
        return { streamPreview: sp };
      });
      break;
    }

    case 'handoff_progress': {
      const message = (p.message as string) || '';
      if (!message) break;
      // [2026-05-17] Progress messages are backend tool execution logs.
      // Only show them in the live streaming preview indicator, NOT as
      // tool call cards in the final message. Tool calls are properly
      // reconstructed from session history via buildChatMessagesFromHistory.
      set(s => {
        const sp = { ...s.streamPreview };
        sp.progressLines = [...sp.progressLines.slice(-5), message];
        sp.isActive = true;
        return { streamPreview: sp };
      });
      break;
    }

    case 'llm_retry': {
      // [2026-06-01] Why: retry preview text used an emoji as a status icon.
      // How: keep the readable retry label but remove the glyph prefix. Purpose:
      // live progress strings no longer reintroduce emoji during the icon migration.
      const info = `重试 ${p.attempt || '?'}/${p.max_retries || '?'} — ${((p.error as string) || '未知错误').slice(0, 80)}`;
      set(s => {
        const sp = { ...s.streamPreview };
        sp.retryInfo = info;
        sp.thinkingPreview = '';
        sp.textPreview = '';
        sp.thinkingStartTime = Date.now();
        sp.isActive = true;
        return { streamPreview: sp };
      });
      break;
    }

    case 'tool_call_start': {
      // [2026-05-17] Engine now emits structured tool start events during the live
      // turn. Store them in the turn accumulator so the next reply or final message
      // can render tool rows immediately instead of waiting for a history reload.
      const toolCall: ToolCall = {
        id: (p.tool_call_id as string) || '',
        name: (p.tool_name as string) || '',
        // [2026-06-01] Why: tool summaries used an hourglass emoji in store state.
        // How: keep the progress summary as plain text. Purpose: status iconography is
        // handled by tool rows rather than persisted strings.
        summary: 'executing...',
        arguments: (p.arguments as Record<string, unknown>) || undefined,
        nodeId: (p.node_id as string) || '',
        status: undefined,
      };
      _pendingTools.push(toolCall);
      set(s => {
        const sp = { ...s.streamPreview };
        // [2026-06-01] Why: progress lines used to persist a wrench emoji in store
        // state. How: store plain tool names and let StreamPreview own any visual
        // iconography. Purpose: application state remains text-only and does not
        // reintroduce emoji when rendered elsewhere.
        sp.progressLines = [...sp.progressLines.slice(-5), toolCall.name];
        sp.isActive = true;
        return { streamPreview: sp };
      });
      break;
    }

    case 'tool_call_end': {
      // [2026-05-17] End events complete the pending tool row in place. This keeps
      // the final assistant message tied to the same call id while showing success
      // or error state as soon as the backend reports it.
      const callId = (p.tool_call_id as string) || '';
      const rawStatus = (p.status as string) || 'success';
      const status = (rawStatus === 'error' || rawStatus === 'cancelled') ? 'error' : 'success';
      const summary = (p.summary as string) || '';
      const idx = _pendingTools.findIndex(t => t.id === callId);
      if (idx >= 0) {
        _pendingTools[idx] = {
          ..._pendingTools[idx],
          status: status as 'success' | 'error',
          summary,
        };
      }
      break;
    }

    case 'outbound_message': {
      const text = (p.text as string) || '';
      const atts = (p.attachments as Attachment[] | undefined) || [];
      // [2026-05-17] Attach live structured tool calls collected from
      // tool_call_start/tool_call_end to the final message. This is the realtime
      // counterpart of buildChatMessagesFromHistory and prevents missing tool rows
      // before a manual history reload.
      const assistantMsg: ChatMessage = {
        id: createMessageId('assistant'),
        conversationId,
        role: 'assistant',
        content: text,
        createdAt: new Date().toISOString(),
        attachments: atts.length > 0 ? atts : undefined,
        toolCalls: _pendingTools.length > 0 ? [..._pendingTools] : undefined,
        thinking: _pendingThinking || undefined,
      };
      resetTurnAccumulator();
      set(s => {
        const convs = updateMessages(s.conversations, conversationId, msgs => [...msgs, assistantMsg]);
        saveConversations(convs);
        return {
          conversations: convs,
          activeConversation: findActive(convs, conversationId),
          typingConversationId: null,
          isGenerating: false,
          streamPreview: { ...emptyPreview },
        };
      });
      stopPolling();
      break;
    }

    case 'intermediate_reply': {
      const text = (p.text as string) || '';
      if (!text) break;
      const replyMsg: ChatMessage = {
        id: createMessageId('reply'),
        conversationId,
        role: 'assistant',
        content: text,
        createdAt: new Date().toISOString(),
        isIntermediate: true,
        toolCalls: _pendingTools.length > 0 ? [..._pendingTools] : undefined,
        thinking: _pendingThinking || undefined,
      };
      // [2026-05-17] Intermediate replies consume the tools collected so far,
      // because those rows now belong to this visible partial answer. Thinking is
      // intentionally kept because later replies and the final answer may continue it.
      _pendingTools = [];
      set(s => {
        const convs = updateMessages(s.conversations, conversationId, msgs => [...msgs, replyMsg]);
        saveConversations(convs);
        return { conversations: convs, activeConversation: findActive(convs, conversationId) };
      });
      break;
    }

    case 'approval_requested': {
      const approvalMsg: ChatMessage = {
        id: createMessageId('approval'),
        conversationId,
        role: 'system',
        content: '',
        createdAt: new Date().toISOString(),
        approval: {
          id: (p.approval_id as string) || '',
          operation: (p.operation as string) || '',
          details: (p.details as any) || {},
          status: 'pending',
        },
      };
      set(s => {
        const convs = updateMessages(s.conversations, conversationId, msgs => [...msgs, approvalMsg]);
        return { conversations: convs, activeConversation: findActive(convs, conversationId) };
      });
      break;
    }

    case 'task_created': {
      const taskId = (p.task_id as string) || '';
      if (taskId) set(() => ({ currentTaskId: taskId }));
      break;
    }

    case 'task_completed':
    case 'task_failed': {
      // If no outbound_message arrived, stop generating anyway
      resetTurnAccumulator();
      set(s => {
        if (!s.isGenerating) return {};
        return {
          isGenerating: false,
          typingConversationId: null,
          streamPreview: { ...emptyPreview },
        };
      });
      stopPolling();
      break;
    }

    default:
      // Ignore unknown events
      break;
  }
}

// ── Load history from Supervisor ──

const FINAL_TEXT_TOOL_NAMES = new Set(['finish', 'reply', 'switch_node']);
const INTERNAL_USER_MESSAGE_TYPES = new Set(['tool_result', 'system', 'summary']);

type ToolResultInfo = {
  status: 'success' | 'error';
  result?: string;
  isAutoResult?: boolean;
  rejected?: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function normalizeRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
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

function formatArgumentValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value == null || typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function summarizeToolArguments(toolName: string, args: Record<string, unknown>): string {
  // [2026-05-17] finish/reply text is already promoted to ChatMessage.content;
  // repeating it as a parameter summary was the source of duplicated user-facing text.
  if (FINAL_TEXT_TOOL_NAMES.has(toolName) && typeof args.text === 'string') return '';

  const entries = Object.entries(args);
  if (entries.length === 0) return '';

  return entries
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${truncateForPreview(collapseForPreview(formatArgumentValue(value)), 80)}`)
    .join(', ');
}

function buildToolResultIndex(messages: StructuredMessage[]): Map<string, ToolResultInfo> {
  const resultIndex = new Map<string, ToolResultInfo>();

  for (const message of messages) {
    if (message.role !== 'tool' || !message.tool_call_id) continue;

    const toolName = message.tool_name || message.name || '';
    const rawContent = stringifyContent(message.content);
    const trimmedStart = rawContent.trimStart();
    // [2026-06-01] Why: historical tool results may still contain old cross-mark
    // prefixes, but new frontend strings should not add them. How: keep the legacy
    // parser for stored data while also accepting plain rejected text. Purpose: the UI
    // can migrate icons without breaking old conversations.
    const rejected = /^\s*(?:❌\s*)?REJECTED(?:\b|:)/i.test(trimmedStart);
    const status: ToolResultInfo['status'] = rejected || /^ERROR(?:\b|:)/i.test(trimmedStart) ? 'error' : 'success';
    const isAutoResult = status === 'success'
      && FINAL_TEXT_TOOL_NAMES.has(toolName)
      && rawContent.trim().toLowerCase() === 'ok';

    // [2026-05-17] Clonoth stores tool responses as separate flat messages. This
    // index is the replacement for the previous history hack: calls are matched by
    // tool_call_id, automatic finish/reply "ok" content is hidden, rejected errors
    // remain complete, and normal results become short summaries only.
    resultIndex.set(message.tool_call_id, {
      status,
      isAutoResult: isAutoResult || undefined,
      rejected: rejected || undefined,
      result: isAutoResult
        ? undefined
        : status === 'error'
          ? rawContent
          : truncateForPreview(collapseForPreview(rawContent), 120) || undefined,
    });
  }

  return resultIndex;
}

function parseAssistantToolCalls(message: StructuredMessage, resultIndex: Map<string, ToolResultInfo>): ToolCall[] {
  if (!message.tool_calls || message.tool_calls.length === 0) return [];

  return message.tool_calls.map((toolCall, index) => {
    const name = toolCall.name || 'unknown';
    const args = normalizeRecord(toolCall.arguments);
    const fallbackId = message.id ? `${message.id}:tool:${index}` : `tool:${index}`;
    const callId = toolCall.id || fallbackId;
    const result = toolCall.id ? resultIndex.get(toolCall.id) : undefined;

    return {
      id: callId,
      name,
      summary: summarizeToolArguments(name, args),
      arguments: args,
      nodeId: message.source_node_id || undefined,
      status: result?.status,
      result: result?.result,
      isAutoResult: result?.isAutoResult,
      rejected: result?.rejected,
    };
  });
}

function extractFinalToolText(message: StructuredMessage): { text: string; toolName?: string } {
  for (const toolCall of message.tool_calls || []) {
    if (!FINAL_TEXT_TOOL_NAMES.has(toolCall.name)) continue;
    const args = normalizeRecord(toolCall.arguments);
    if (typeof args.text === 'string') {
      return { text: args.text, toolName: toolCall.name };
    }
  }
  return { text: '' };
}

function joinThinking(parts: string[]): string | undefined {
  const filtered = parts.filter(part => part.trim().length > 0);
  return filtered.length > 0 ? filtered.join('\n---\n') : undefined;
}

export function buildChatMessagesFromHistory(serverMsgs: StructuredMessage[], conversationId: string): ChatMessage[] {
  // [2026-05-17] The Supervisor history is flat, while the UI needs Lim-Code-like
  // call/response pairs. This pure mapper performs the pairing once and returns only
  // user/assistant display messages, so raw role=tool rows never become chat bubbles.
  const resultIndex = buildToolResultIndex(serverMsgs);
  const converted: ChatMessage[] = [];
  let accumulatedThinking: string[] = [];
  let accumulatedTools: ToolCall[] = [];
  let accumulatedId = '';
  let accumulatedCreatedAt = '';

  const resetAccumulatedAssistant = () => {
    accumulatedThinking = [];
    accumulatedTools = [];
    accumulatedId = '';
    accumulatedCreatedAt = '';
  };

  const rememberAssistantPrefix = (message: StructuredMessage, toolCalls: ToolCall[]) => {
    // [2026-05-17] Tool-only assistant messages are not useful as empty bubbles.
    // They are carried forward and merged into the next textual finish/reply turn.
    if (!accumulatedId) accumulatedId = message.id || '';
    if (!accumulatedCreatedAt) accumulatedCreatedAt = message.created_at || '';
    if (message.thinking) accumulatedThinking.push(message.thinking);
    if (toolCalls.length > 0) accumulatedTools.push(...toolCalls);
  };

  const flushDanglingAssistant = () => {
    if (accumulatedThinking.length === 0 && accumulatedTools.length === 0) return;

    converted.push({
      id: accumulatedId || `srv-acc-${converted.length}`,
      conversationId,
      role: 'assistant',
      content: '',
      createdAt: accumulatedCreatedAt || new Date().toISOString(),
      thinking: joinThinking(accumulatedThinking),
      toolCalls: accumulatedTools.length > 0 ? [...accumulatedTools] : undefined,
    });
    resetAccumulatedAssistant();
  };

  for (const message of serverMsgs) {
    if (message.role === 'user') {
      flushDanglingAssistant();
      if (INTERNAL_USER_MESSAGE_TYPES.has(message.message_type || '')) continue;
      if (typeof message.content !== 'string') continue;

      converted.push({
        id: message.id || `srv-${conversationId.slice(0, 8)}-${converted.length}`,
        conversationId,
        role: 'user',
        content: message.content,
        createdAt: message.created_at || new Date().toISOString(),
      });
      continue;
    }

    if (message.role !== 'assistant') {
      continue;
    }

    const currentTools = parseAssistantToolCalls(message, resultIndex);
    const finalTool = extractFinalToolText(message);
    const contentText = typeof message.content === 'string' ? message.content : stringifyContent(message.content);
    const displayText = contentText.trim().length > 0 ? contentText : finalTool.text;
    const hasFinalTool = (message.tool_calls || []).some(toolCall => FINAL_TEXT_TOOL_NAMES.has(toolCall.name));

    if (!displayText && !hasFinalTool) {
      rememberAssistantPrefix(message, currentTools);
      continue;
    }

    const thinking = joinThinking([
      ...accumulatedThinking,
      ...(message.thinking ? [message.thinking] : []),
    ]);
    const toolCalls = [...accumulatedTools, ...currentTools];

    converted.push({
      id: message.id || accumulatedId || `srv-${conversationId.slice(0, 8)}-${converted.length}`,
      conversationId,
      role: 'assistant',
      content: displayText,
      createdAt: message.created_at || accumulatedCreatedAt || new Date().toISOString(),
      thinking,
      toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
      isIntermediate: finalTool.toolName === 'reply' || undefined,
    });
    resetAccumulatedAssistant();
  }

  flushDanglingAssistant();
  return converted;
}

async function loadSessionHistory(
  conversationId: string,
  sessionId: string,
  set: (fn: (s: ChatState) => Partial<ChatState>) => void,
  get: () => ChatState,
) {
  let serverMsgs: StructuredMessage[];
  try {
    serverMsgs = await getSessionHistory(sessionId, 200);
  } catch {
    return;
  }
  if (!serverMsgs || serverMsgs.length === 0) return;

  const converted = buildChatMessagesFromHistory(serverMsgs, conversationId);

  // Always replace local messages with server data (server is source of truth)
  set(s => {
    const conv = s.conversations.find(c => c.id === conversationId);
    if (!conv) return {};
    const convs = s.conversations.map(c =>
      c.id !== conversationId ? c : { ...c, messages: converted, updatedAt: new Date().toISOString() },
    );
    return {
      conversations: convs,
      activeConversation: findActive(convs, s.activeConversationId),
    };
  });
}

export type { ChatState };
