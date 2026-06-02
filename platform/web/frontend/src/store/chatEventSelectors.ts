// [2026-05-31] Legacy selector compatibility for older chatEventReducer tests.
// Why: the project now uses eventSelectors.ts and the normalized RenderBlock model,
// but one historical test still asserts the previous block projection. How: delegate
// to the new selectors, then map only the old surface fields. Purpose: avoid a second
// source of reducer truth while keeping obsolete imports harmless during migration.
import type { EventLogEntry, RenderBlock, WsMessage } from '../types/message';
import { selectEventLog as selectReducerEventLog, selectMessages as selectReducerMessages, selectToolExecutions } from './eventSelectors';
import type { ChatEventReducerCompatState } from './chatEventReducer';

type CompatBlock =
  | { type: 'text'; text: string; delivery: string; status: 'done' | 'streaming' }
  | { type: 'thinking'; text: string; status: 'done' | 'streaming'; startedAt: number; endedAt?: number }
  | { type: 'tool'; toolIds: readonly string[] }
  | { type: 'approval'; approvalId: string }
  | { type: 'notice'; text: string; level: string };

type CompatMessage = Omit<WsMessage, 'blocks'> & { blocks: CompatBlock[] };
type CompatEventLogEntry = EventLogEntry & { severity: 'info' | 'warning' | 'error' };

function toCompatBlock(block: RenderBlock, state: ChatEventReducerCompatState): CompatBlock {
  if (block.kind === 'text') {
    return { type: 'text', text: block.text, delivery: block.delivery, status: block.streaming ? 'streaming' : 'done' };
  }
  if (block.kind === 'thinking') {
    return {
      type: 'thinking',
      text: block.text,
      status: block.streaming ? 'streaming' : 'done',
      startedAt: Date.parse(block.createdAt),
      endedAt: block.streaming ? undefined : Date.parse(block.updatedAt),
    };
  }
  if (block.kind === 'tool') {
    return {
      type: 'tool',
      toolIds: block.toolIds.filter((toolId) => !state.toolExecutionsById[toolId]?.hidden),
    };
  }
  if (block.kind === 'approval') return { type: 'approval', approvalId: block.approvalId };
  return { type: 'notice', text: block.text, level: block.level };
}

function projectBlocksForLegacyTests(message: WsMessage, state: ChatEventReducerCompatState): CompatBlock[] {
  const finalTextBlock = [...message.blocks].reverse().find((block) => block.kind === 'text' && block.delivery === 'final');
  const blocks = finalTextBlock
    ? message.blocks.filter((block) => block.kind !== 'text' || block.id === finalTextBlock.id)
    : message.blocks;

  // Why: the obsolete selector shape exposed only a single final text block for a
  // completed assistant turn. How: when a final text block exists, drop earlier stream
  // text blocks from this compatibility projection only. Purpose: keep old tests from
  // dictating the new richer RenderBlock model.
  return blocks.map((block) => toCompatBlock(block, state));
}

export function selectMessages(state: ChatEventReducerCompatState, conversationId: string): CompatMessage[] {
  return selectReducerMessages(state, conversationId).map((message) => ({
    ...message,
    blocks: projectBlocksForLegacyTests(message, state),
  }));
}

export function selectToolCalls(state: ChatEventReducerCompatState, toolIds: readonly string[]) {
  return selectToolExecutions(state, toolIds);
}

export function selectEventLog(state: ChatEventReducerCompatState, sessionId: string, limit = 200): CompatEventLogEntry[] {
  return selectReducerEventLog(state, sessionId, limit).map((entry) => ({
    ...entry,
    severity: entry.type.includes('error') || entry.type.includes('failed') ? 'error' : 'info',
  }));
}
