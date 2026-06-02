// [2026-05-31] Legacy compatibility wrapper for the earlier Step 1 reducer names.
// Why: an older focused test file still imports chatEventReducer/chatEventSelectors,
// while the accepted reducer now lives in eventReducer.ts. How: seed the new ChatState
// with the old active conversation/session hints, delegate every event to the new
// reducer, and expose derived legacy aliases. Purpose: keep historical tests and any
// temporary imports working without creating a second event-replay implementation.
import type { SupervisorEvent } from '../types';
import type { ApprovalBlock, ChatState, ToolExecution } from '../types/message';
import { createInitialChatState, reduceChatEvent as reduceReducerEvent } from './eventReducer';

interface InitialChatStateV2Options {
  activeConversationId?: string | null;
  sessionMap?: Record<string, string>;
}

type CompatToolExecution = Omit<ToolExecution, 'control'> & { control?: string | boolean };
type CompatApprovalBlock = Omit<ApprovalBlock, 'id'> & { id: string };

export interface ChatEventReducerCompatState extends ChatState {
  activeConversationId?: string | null;
  sessionMap?: Record<string, string>;
  toolsById: Record<string, CompatToolExecution>;
  approvalsById: Record<string, CompatApprovalBlock>;
}

const CONTROL_TOOL_NAMES = new Set(['finish', 'reply', 'switch_node']);

function buildConversationIdsBySession(sessionMap: Record<string, string> | undefined): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [conversationId, sessionId] of Object.entries(sessionMap || {})) {
    if (sessionId) result[sessionId] = conversationId;
  }
  return result;
}

function withCompatAliases(state: ChatState & InitialChatStateV2Options): ChatEventReducerCompatState {
  const toolsById: Record<string, CompatToolExecution> = {};
  const approvalsById: Record<string, CompatApprovalBlock> = {};

  for (const [id, tool] of Object.entries(state.toolExecutionsById)) {
    // Why: the old tests treated control tools as a named control string, while the
    // new model uses a boolean plus the tool name. How: derive the legacy control
    // field without changing ToolExecution itself. Purpose: keep compatibility local.
    toolsById[id] = CONTROL_TOOL_NAMES.has(tool.name) ? { ...tool, control: tool.name } : { ...tool };
  }

  for (const message of Object.values(state.messagesById)) {
    for (const block of message.blocks) {
      if (block.kind === 'approval') {
        approvalsById[block.approvalId] = {
          ...block,
          // Why: the new ApprovalBlock id is a render-block id, while the old
          // compatibility state keyed and displayed the approval id itself. How:
          // rewrite only this alias object. Purpose: preserve the old assertion
          // shape without changing the reducer-owned block.
          id: block.approvalId,
        };
      }
    }
  }

  return {
    ...state,
    activeConversationId: state.activeConversationId ?? null,
    sessionMap: state.sessionMap || {},
    toolsById,
    approvalsById,
  };
}

export function createInitialChatStateV2(options: InitialChatStateV2Options = {}): ChatEventReducerCompatState {
  return withCompatAliases({
    ...createInitialChatState(),
    conversationIdsBySession: buildConversationIdsBySession(options.sessionMap),
    activeConversationId: options.activeConversationId ?? null,
    sessionMap: options.sessionMap || {},
  });
}

export function reduceChatEvent(
  state: ChatEventReducerCompatState,
  event: SupervisorEvent,
): ChatEventReducerCompatState {
  const nextState = reduceReducerEvent(state, event);
  if (nextState === state) return state;

  return withCompatAliases({
    ...nextState,
    activeConversationId: state.activeConversationId ?? null,
    sessionMap: state.sessionMap || {},
  });
}
