// [Chat V2 reducer tests 2026-05-31]
// Why: Step 1 introduces a pure WebSocket event reducer that is not connected to
// the current chatStore yet, so focused unit tests protect the replay contract
// before any UI wiring is added. How: these tests feed representative Supervisor
// events into the new reducer and assert message, tool, approval, and de-dupe
// state. Purpose: keep the new reducer deterministic while the legacy chat flow
// continues to coexist unchanged.
import { describe, expect, it } from 'vitest';

import { createInitialChatStateV2, reduceChatEvent } from '../store/chatEventReducer';
import { selectEventLog, selectMessages, selectToolCalls } from '../store/chatEventSelectors';
import type { SupervisorEvent } from '../types';

function event(seq: number, type: string, payload: Record<string, unknown>): SupervisorEvent {
  return {
    seq,
    event_id: `event-${seq}`,
    ts: `2026-05-31T00:00:${String(seq).padStart(2, '0')}Z`,
    session_id: 'session-1',
    type,
    component: 'test',
    payload,
  };
}

describe('chatEventReducer', () => {
  it('creates separate streamed and outbound assistant cards', () => {
    let state = createInitialChatStateV2({
      activeConversationId: 'conversation-1',
      sessionMap: { 'conversation-1': 'session-1' },
    });

    state = reduceChatEvent(state, event(1, 'inbound_message', {
      conversation_key: 'web:conversation-1',
      text: 'Hello',
      attachments: [{ name: 'note.txt', size: 4, url: '/files/note.txt' }],
    }));
    state = reduceChatEvent(state, event(2, 'stream_delta', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      type: 'thinking',
      content: 'plan',
    }));
    state = reduceChatEvent(state, event(3, 'stream_delta', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      type: 'text',
      content: 'draft',
    }));
    state = reduceChatEvent(state, event(4, 'stream_end', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      has_text: true,
      has_reasoning: true,
    }));
    state = reduceChatEvent(state, event(5, 'outbound_message', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      text: 'final answer',
    }));

    const messages = selectMessages(state, 'conversation-1');
    expect(messages).toHaveLength(3);
    expect(messages[0].role).toBe('user');
    expect(messages[0].blocks[0]).toMatchObject({ type: 'text', text: 'Hello', delivery: 'final', status: 'done' });
    expect(messages[0].attachments).toHaveLength(1);
    expect(messages[1].status).toBe('completed');
    // Why: the compatibility selector now projects the new reducer behavior where
    // outbound_message has its own assistant card. How: assert the original card keeps
    // finalized stream blocks and the outbound card contains only final text. Purpose:
    // old compatibility tests no longer force final text to merge into stream content.
    expect(messages[1].blocks).toEqual([
      { type: 'thinking', text: 'plan', status: 'done', startedAt: Date.parse('2026-05-31T00:00:02Z'), endedAt: Date.parse('2026-05-31T00:00:04Z') },
      { type: 'text', text: 'draft', delivery: 'stream', status: 'done' },
    ]);
    expect(messages[2].status).toBe('completed');
    expect(messages[2].blocks).toEqual([
      { type: 'text', text: 'final answer', delivery: 'final', status: 'done' },
    ]);
  });

  it('merges tool lifecycle events and hides successful control tools', () => {
    let state = createInitialChatStateV2({
      activeConversationId: 'conversation-1',
      sessionMap: { 'conversation-1': 'session-1' },
    });

    state = reduceChatEvent(state, event(10, 'tool_call_delta', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      event: 'tool_call_start',
      index: 0,
      id: 'call-search',
      name: 'search_in_files',
    }));
    state = reduceChatEvent(state, event(11, 'tool_call_delta', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      event: 'tool_call_args_delta',
      index: 0,
      delta: '{"query":"needle"}',
    }));
    state = reduceChatEvent(state, event(12, 'tool_call_start', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      tool_call_id: 'call-search',
      tool_name: 'search_in_files',
      arguments: { query: 'needle' },
    }));
    state = reduceChatEvent(state, event(13, 'tool_call_end', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      tool_call_id: 'call-search',
      tool_name: 'search_in_files',
      status: 'success',
      summary: 'found one match',
      result: { count: 1 },
      raw_inline: 'found one match',
      format: 'text',
      elapsed_ms: 12.5,
    }));
    state = reduceChatEvent(state, event(14, 'tool_call_start', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      tool_call_id: 'call-finish',
      tool_name: 'finish',
      arguments: { text: 'done' },
    }));
    state = reduceChatEvent(state, event(15, 'tool_call_end', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      node_id: 'node-1',
      tool_call_id: 'call-finish',
      tool_name: 'finish',
      status: 'success',
      summary: 'ok',
    }));

    const messages = selectMessages(state, 'conversation-1');
    const toolBlock = messages[0].blocks.find(block => block.type === 'tool');
    expect(toolBlock).toMatchObject({ type: 'tool', toolIds: ['message:conversation-1:assistant:inbound:1|tool:id:call-search'] });

    const tools = selectToolCalls(state, toolBlock?.type === 'tool' ? toolBlock.toolIds : []);
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({
      id: 'call-search',
      index: 0,
      name: 'search_in_files',
      status: 'success',
      arguments: { query: 'needle' },
      argumentsText: '{"query":"needle"}',
      summary: 'found one match',
      rawInline: 'found one match',
      format: 'text',
      elapsedMs: 12.5,
    });
    expect(Object.values(state.toolsById).some(tool => tool.name === 'finish' && tool.control === 'finish')).toBe(true);
    expect(tools.some(tool => tool.name === 'finish')).toBe(false);
  });

  it('deduplicates events and keeps handoff progress in the event log only', () => {
    let state = createInitialChatStateV2({
      activeConversationId: 'conversation-1',
      sessionMap: { 'conversation-1': 'session-1' },
    });
    const progress = event(20, 'handoff_progress', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      message: '[node] executing 1 tool',
    });

    state = reduceChatEvent(state, progress);
    const sameReference = reduceChatEvent(state, progress);

    expect(sameReference).toBe(state);
    expect(selectMessages(state, 'conversation-1')).toHaveLength(0);
    expect(selectEventLog(state, 'session-1', 10)).toMatchObject([
      { eventId: 'event-20', type: 'handoff_progress', summary: '[node] executing 1 tool', severity: 'info' },
    ]);
  });

  it('stores approval requests and updates their decision state', () => {
    let state = createInitialChatStateV2({
      activeConversationId: 'conversation-1',
      sessionMap: { 'conversation-1': 'session-1' },
    });

    state = reduceChatEvent(state, event(30, 'approval_requested', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      approval_id: 'approval-1',
      operation: 'write_file',
      details: { path: 'src/file.ts' },
      status: 'pending',
    }));
    state = reduceChatEvent(state, event(31, 'approval_decided', {
      source_inbound_seq: 1,
      task_id: 'task-1',
      approval_id: 'approval-1',
      decision: 'allow',
      comment: 'approved',
    }));

    const messages = selectMessages(state, 'conversation-1');
    expect(messages[0].blocks).toEqual([{ type: 'approval', approvalId: 'approval-1' }]);
    expect(state.approvalsById['approval-1']).toMatchObject({
      id: 'approval-1',
      operation: 'write_file',
      details: { path: 'src/file.ts' },
      status: 'allowed',
    });
  });
});
