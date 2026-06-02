// [2026-05-31] Step 1 tests for the WebSocket-driven chat reducer.
// Why: the new reducer must be replayable and idempotent before any UI is rewired.
// How: model real SupervisorEvent payloads from backend code paths and assert only
// derived ChatState changes. Purpose: prevent the refactor from recreating the old
// split between live stream preview and final message bubbles.
import { describe, expect, it } from 'vitest';

import type { SupervisorEvent } from '../types/chat';
import { createInitialChatState, reduceSupervisorEvent, replaySupervisorEvents } from '../store/eventReducer';
import { selectEventLog, selectMessages, selectToolExecutions } from '../store/eventSelectors';

function event(seq: number, type: string, payload: Record<string, unknown>, sessionId = 'sess-1'): SupervisorEvent {
  return {
    seq,
    event_id: `ev-${seq}`,
    ts: `2026-05-31T02:43:${String(seq).padStart(2, '0')}.000Z`,
    session_id: sessionId,
    component: 'shell',
    type,
    payload,
  };
}

describe('eventReducer', () => {
  it('creates one user message from inbound_message and ignores duplicate events', () => {
    const inbound = event(1, 'inbound_message', {
      conversation_key: 'web:conv-1',
      text: 'hello',
      attachments: [{ name: 'a.txt', size: 3, url: '/a.txt', type: 'file' }],
    });

    const once = reduceSupervisorEvent(createInitialChatState(), inbound);
    const twice = reduceSupervisorEvent(once, inbound);

    expect(twice).toBe(once);
    expect(selectEventLog(twice, 'sess-1', 10)).toHaveLength(1);

    const messages = selectMessages(twice, 'conv-1');
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ role: 'user', status: 'completed', sessionId: 'sess-1' });
    expect(messages[0].attachments).toHaveLength(1);
    expect(messages[0].blocks[0]).toMatchObject({ kind: 'text', text: 'hello', delivery: 'final' });
  });

  it('merges stream, tool lifecycle, final outbound text, and hides successful control tools', () => {
    const events = [
      event(1, 'inbound_message', { conversation_key: 'web:conv-2', text: 'run uname' }),
      event(2, 'task_created', {
        task_id: 'task-1',
        status: 'pending',
        node_id: 'ereuna_main',
        source_inbound_seq: 1,
        input: { parent_session_id: 'sess-1', branch_session_id: 'branch-1' },
      }),
      event(3, 'node_started', {
        task_id: 'task-1',
        node_id: 'ereuna_main',
        node_name: 'EreunaMain',
        source_inbound_seq: 1,
      }),
      event(4, 'stream_delta', { source_inbound_seq: 1, task_id: 'task-1', node_id: 'ereuna_main', type: 'thinking', content: 'thinking...' }),
      event(5, 'stream_delta', { source_inbound_seq: 1, task_id: 'task-1', node_id: 'ereuna_main', type: 'text', content: 'partial' }),
      event(6, 'stream_end', { source_inbound_seq: 1, task_id: 'task-1', node_id: 'ereuna_main', has_text: true, has_reasoning: true }),
      event(7, 'tool_call_delta', { source_inbound_seq: 1, task_id: 'task-1', event: 'tool_call_start', index: 0, id: 'call-1', name: 'execute_command' }),
      event(8, 'tool_call_delta', { source_inbound_seq: 1, task_id: 'task-1', event: 'tool_call_args_delta', index: 0, delta: '{"command":"uname' }),
      event(9, 'tool_call_delta', { source_inbound_seq: 1, task_id: 'task-1', event: 'tool_call_args_delta', index: 0, delta: ' -s"}' }),
      event(10, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-1',
        node_id: 'ereuna_main',
        tool_call_id: 'call-1',
        tool_name: 'execute_command',
        arguments: { command: 'uname -s' },
      }),
      event(11, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-1',
        node_id: 'ereuna_main',
        tool_call_id: 'call-1',
        tool_name: 'execute_command',
        status: 'success',
        summary: 'returncode=0 Linux',
        result: { ok: true },
        raw_inline: 'returncode=0\nLinux\n',
        format: 'text',
        elapsed_ms: 12.5,
      }),
      event(12, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-1',
        node_id: 'ereuna_main',
        tool_call_id: 'call-finish',
        tool_name: 'finish',
        arguments: { text: 'done' },
      }),
      event(13, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-1',
        node_id: 'ereuna_main',
        tool_call_id: 'call-finish',
        tool_name: 'finish',
        status: 'success',
        summary: '',
        raw_inline: 'ok',
      }),
      event(14, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-1',
        node_id: 'ereuna_main',
        tool_call_id: 'call-ask',
        tool_name: 'ask',
        arguments: { text: 'need input' },
      }),
      event(15, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-1',
        node_id: 'ereuna_main',
        tool_call_id: 'call-ask',
        tool_name: 'ask',
        status: 'success',
        summary: '',
        raw_inline: 'ok',
      }),
      event(16, 'outbound_message', { source_inbound_seq: 1, task_id: 'task-1', node_id: 'ereuna_main', text: 'done', attachments: [] }),
      event(17, 'handoff_progress', { source_inbound_seq: 1, task_id: 'task-1', message: '[ereuna_main] done' }),
      event(18, 'task_completed', { source_inbound_seq: 1, task_id: 'task-1', node_id: 'ereuna_main', status: 'completed' }),
    ];

    const state = replaySupervisorEvents(events, createInitialChatState());
    const messages = selectMessages(state, 'conv-2');

    expect(messages).toHaveLength(3);
    const assistant = messages[1];
    const outbound = messages[2];
    expect(assistant.status).toBe('completed');
    expect(outbound.status).toBe('completed');
    expect(assistant.source).toMatchObject({ inboundSeq: 1, taskId: 'task-1', nodeId: 'ereuna_main', nodeName: 'EreunaMain' });
    expect(outbound.source).toMatchObject({ inboundSeq: 1, taskId: 'task-1', nodeId: 'ereuna_main' });
    // Why: outbound_message must now become its own assistant card instead of being
    // merged into the work-in-progress tool message. How: the original message keeps
    // finalized stream/tool blocks, while the outbound card contains only final text.
    // Purpose: users can inspect intermediate work without mixing it into final output.
    expect(assistant.blocks.map((block) => block.kind)).toEqual(['thinking', 'text', 'tool']);
    expect(assistant.blocks[0]).toMatchObject({ kind: 'thinking', text: 'thinking...', streaming: false });
    expect(assistant.blocks[1]).toMatchObject({ kind: 'text', text: 'partial', delivery: 'stream', streaming: false });
    expect(outbound.blocks).toHaveLength(1);
    expect(outbound.blocks[0]).toMatchObject({ kind: 'text', text: 'done', delivery: 'final', streaming: false });

    const toolBlock = assistant.blocks.find((block) => block.kind === 'tool');
    expect(toolBlock?.kind).toBe('tool');
    const tools = toolBlock?.kind === 'tool' ? selectToolExecutions(state, toolBlock.toolIds) : [];
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({ id: 'call-1', name: 'execute_command', status: 'success', elapsedMs: 12.5 });
    expect(tools[0].argumentsText).toBe('{"command":"uname -s"}');
    expect(tools[0].arguments).toEqual({ command: 'uname -s' });

    expect(Object.values(state.toolExecutionsById).some((tool) => tool.name === 'finish' && tool.hidden)).toBe(true);
    expect(Object.values(state.toolExecutionsById).some((tool) => tool.name === 'ask' && tool.hidden)).toBe(true);
    expect(selectEventLog(state, 'sess-1', 20).map((entry) => entry.type)).toContain('handoff_progress');
  });

  it('keeps streamed text on the original card and creates an outbound card', () => {
    // Why: outbound_message is now a separate assistant card. How: replay only the
    // minimal inbound, stream, and outbound sequence. Purpose: final output is not
    // merged into the earlier streaming card, while the stream block is still closed.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-final', text: 'hello' }),
      event(2, 'stream_delta', { source_inbound_seq: 1, type: 'text', content: 'draft answer' }),
      event(3, 'stream_end', { source_inbound_seq: 1, has_text: true }),
      event(4, 'outbound_message', { source_inbound_seq: 1, text: 'final answer', attachments: [] }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-final');
    const originalAssistant = messages[1];
    const outboundAssistant = messages[2];
    const originalTextBlocks = originalAssistant.blocks.filter((block) => block.kind === 'text');

    expect(messages).toHaveLength(3);
    expect(originalAssistant.status).toBe('completed');
    expect(originalTextBlocks).toHaveLength(1);
    expect(originalTextBlocks[0]).toMatchObject({ kind: 'text', text: 'draft answer', delivery: 'stream', streaming: false });
    expect(outboundAssistant.status).toBe('completed');
    expect(outboundAssistant.blocks).toHaveLength(1);
    expect(outboundAssistant.blocks[0]).toMatchObject({ kind: 'text', text: 'final answer', delivery: 'final', streaming: false });
  });

  it('creates outbound_message separately from the previous tool-call message', () => {
    // Why: a final reply used to merge into the same assistant message that held tool
    // calls. How: replay a tool-only assistant card followed by outbound_message.
    // Purpose: the reducer must preserve the tool card and create a separate final card.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-outbound-tool', text: 'search' }),
      event(2, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-tool',
        tool_call_id: 'call-search',
        tool_name: 'search_in_files',
        arguments: { query: 'needle' },
      }),
      event(3, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-tool',
        tool_call_id: 'call-search',
        tool_name: 'search_in_files',
        status: 'success',
        result: { success: true, data: { results: [], count: 0, truncated: false } },
        format: 'json',
      }),
      event(4, 'outbound_message', { source_inbound_seq: 1, task_id: 'task-tool', text: 'final answer' }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-outbound-tool');
    const toolMessage = messages[1];
    const outboundMessage = messages[2];

    expect(messages).toHaveLength(3);
    expect(toolMessage.blocks.map((block) => block.kind)).toEqual(['tool']);
    expect(outboundMessage.blocks).toHaveLength(1);
    expect(outboundMessage.blocks[0]).toMatchObject({ kind: 'text', text: 'final answer', delivery: 'final' });
  });

  it('caps reducer event bookkeeping for long sessions', () => {
    // Why: replay can receive thousands of events during reconnect catch-up. How:
    // this test drives the reducer past the configured bookkeeping limits. Purpose:
    // old audit rows and idempotency keys must not grow without bound in the browser.
    const initial = createInitialChatState();
    const eventLog = Array.from({ length: 3000 }, (_, index) => {
      const seq = index + 1;
      return {
        id: `log:ev-${seq}`,
        eventId: `ev-${seq}`,
        seq,
        ts: `2026-05-31T02:43:${String(seq % 60).padStart(2, '0')}.000Z`,
        sessionId: 'sess-1',
        conversationId: 'sess-1',
        type: 'handoff_progress',
        payload: { message: `step ${seq}` },
      };
    });
    const processedEventIds = Object.fromEntries(
      Array.from({ length: 5000 }, (_, index) => [`ev-${index + 1}`, true] as const),
    );

    const state = reduceSupervisorEvent({ ...initial, eventLog, processedEventIds }, event(5001, 'handoff_progress', { message: 'step 5001' }));
    const processedIds = Object.keys(state.processedEventIds);

    expect(state.eventLog).toHaveLength(3000);
    expect(state.eventLog[0].seq).toBe(2);
    expect(processedIds.length).toBeLessThanOrEqual(5000);
    expect(state.processedEventIds['ev-1']).toBeUndefined();
    expect(state.processedEventIds['ev-5001']).toBe(true);
  });

  it('keeps legacy approvals without tool_call_id as independent approval blocks', () => {
    // Why: old backend events do not contain tool_call_id. How: replay the legacy
    // approval_requested/approval_decided shape without any tool event. Purpose: the
    // fallback ApprovalBlock remains visible for historical and mixed-version sessions.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-3', text: 'write file' }),
      event(2, 'approval_requested', {
        source_inbound_seq: 1,
        task_id: 'task-approval',
        approval_id: 'ap-1',
        operation: 'write_file',
        details: { path: 'README.md', reason: 'test' },
        status: 'pending',
      }),
      event(3, 'approval_decided', {
        source_inbound_seq: 1,
        task_id: 'task-approval',
        approval_id: 'ap-1',
        decision: 'deny',
        comment: 'not now',
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-3');
    expect(messages).toHaveLength(2);
    expect(messages[1].status).toBe('running_tools');
    expect(messages[1].blocks[0]).toMatchObject({
      kind: 'approval',
      approvalId: 'ap-1',
      operation: 'write_file',
      status: 'denied',
      decision: 'deny',
      comment: 'not now',
    });
  });



  it('updates a tool approval decision from tool_call_id even without a local approval location', () => {
    // Why: reconnect catch-up may deliver approval_decided with tool_call_id to a
    // reducer state that never saw approval_requested. How: replay only the tool
    // start and decision event. Purpose: the tool card still records the decision
    // without requiring a legacy ApprovalBlock location.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-decision-only', text: 'write file' }),
      event(2, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-decision-only',
        node_id: 'ereuna_main',
        tool_call_id: 'call-write-only',
        tool_name: 'write_file',
        arguments: { path: 'README.md', content: 'next' },
      }),
      event(3, 'approval_decided', {
        source_inbound_seq: 1,
        task_id: 'task-decision-only',
        node_id: 'ereuna_main',
        approval_id: 'ap-decision-only',
        tool_call_id: 'call-write-only',
        decision: 'deny',
        comment: 'no',
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-decision-only');
    const toolBlock = messages[1].blocks[0];
    const tools = toolBlock.kind === 'tool' ? selectToolExecutions(state, toolBlock.toolIds) : [];

    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({
      id: 'call-write-only',
      status: 'error',
      approvalId: 'ap-decision-only',
      approvalStatus: 'denied',
    });
  });

  it('merges approvals with tool_call_id into the matching tool execution', () => {
    // Why: approvals are now a ToolExecution state instead of a separate card when
    // the backend supplies tool_call_id. How: create a tool, request approval for
    // that call id, then approve it. Purpose: the same tool card shows pending and
    // decided approval state while legacy fallback remains available above.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-tool-approval', text: 'write file' }),
      event(2, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-tool-approval',
        node_id: 'ereuna_main',
        tool_call_id: 'call-write',
        tool_name: 'write_file',
        arguments: { path: 'README.md', content: 'next' },
      }),
      event(3, 'approval_requested', {
        source_inbound_seq: 1,
        task_id: 'task-tool-approval',
        node_id: 'ereuna_main',
        approval_id: 'ap-tool-1',
        tool_call_id: 'call-write',
        operation: 'write_file',
        details: { path: 'README.md', reason: 'test' },
        status: 'pending',
      }),
      event(4, 'approval_decided', {
        source_inbound_seq: 1,
        task_id: 'task-tool-approval',
        node_id: 'ereuna_main',
        approval_id: 'ap-tool-1',
        tool_call_id: 'call-write',
        decision: 'allow',
        comment: 'ok',
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-tool-approval');
    expect(messages).toHaveLength(2);
    expect(messages[1].status).toBe('running_tools');
    expect(messages[1].blocks.map((block) => block.kind)).toEqual(['tool']);

    const toolBlock = messages[1].blocks[0];
    const tools = toolBlock.kind === 'tool' ? selectToolExecutions(state, toolBlock.toolIds) : [];
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({
      id: 'call-write',
      status: 'running',
      approvalId: 'ap-tool-1',
      approvalStatus: 'allowed',
      approvalDetails: { operation: 'write_file', details: { path: 'README.md', reason: 'test' } },
    });
    expect(state.approvalBlockById['ap-tool-1']).toMatchObject({
      messageId: messages[1].id,
      blockId: toolBlock.id,
      toolCallId: 'call-write',
    });
  });
});
