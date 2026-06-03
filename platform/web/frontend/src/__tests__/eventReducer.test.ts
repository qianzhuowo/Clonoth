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

  it('creates a dispatch callback message from realtime dispatch_result inbound metadata', () => {
    // [AutoC 2026-06-03] Why: dispatch callbacks arrive through the same
    // inbound_message reducer path as real user messages. How: provide the backend
    // message_type and child_session_id fields in the event payload. Purpose: live
    // WebSocket rendering matches refreshed history and keeps the child jump target.
    const state = reduceSupervisorEvent(createInitialChatState(), event(2, 'inbound_message', {
      conversation_key: 'web:conv-dispatch-live',
      text: '[异步子任务完成] parent 委派的 scout 已完成。',
      message_type: 'dispatch_result',
      task_id: 'task-child',
      node_id: 'scout',
      child_session_id: 'child-scout',
    }));

    const messages = selectMessages(state, 'conv-dispatch-live');
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ role: 'dispatch_callback', status: 'completed' });
    expect(messages[0].source).toMatchObject({ taskId: 'task-child', nodeId: 'scout', childSessionId: 'child-scout' });
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

  it('marks intermediate replies as reply completions during stream replay', () => {
    // [2026-06-02] Why: MessageCard now applies reply and finish borders from
    // completionType at the message level. How: intermediate_reply must mark the
    // assistant message as a reply while keeping its text block delivery intermediate.
    // Purpose: live reply output receives the blue assistant-only border without
    // relying on TextBlockView block-level styling.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-intermediate-reply', text: 'continue' }),
      event(2, 'intermediate_reply', {
        source_inbound_seq: 1,
        task_id: 'task-intermediate-reply',
        node_id: 'ereuna_main',
        text: 'partial answer',
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-intermediate-reply');
    expect(messages).toHaveLength(2);
    expect(messages[1]).toMatchObject({ role: 'assistant', status: 'running_tools', completionType: 'reply' });
    expect(messages[1].blocks[0]).toMatchObject({ kind: 'text', text: 'partial answer', delivery: 'intermediate' });
  });


  it('keeps same-round tools on the reply card when intermediate_reply arrives before tool ends', () => {
    // [2026-06-02] Regression for same-round tool splitting.
    // Why: intermediate_reply marks the assistant card as a reply before other tools
    // from the same LLM request have ended. How: replay reply and mcp_time tools with
    // tool_call_end events after intermediate_reply. Purpose: tool lifecycle events do
    // not create a new card until a later stream_delta starts the next LLM round.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-reply-same-round-tools', text: 'tell me time' }),
      event(2, 'task_created', {
        source_inbound_seq: 1,
        task_id: 'task-reply-same-round-tools',
        node_id: 'ereuna_main',
      }),
      event(3, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-reply-same-round-tools',
        node_id: 'ereuna_main',
        tool_call_id: 'call-reply',
        tool_name: 'reply',
        arguments: { text: '我先回复。' },
      }),
      event(4, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-reply-same-round-tools',
        node_id: 'ereuna_main',
        tool_call_id: 'call-time',
        tool_name: 'mcp_time_current_time',
        arguments: { timezone: 'UTC' },
      }),
      event(5, 'intermediate_reply', {
        source_inbound_seq: 1,
        task_id: 'task-reply-same-round-tools',
        node_id: 'ereuna_main',
        text: '我先回复。',
      }),
      event(6, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-reply-same-round-tools',
        node_id: 'ereuna_main',
        tool_call_id: 'call-reply',
        tool_name: 'reply',
        status: 'success',
        raw_inline: 'ok',
      }),
      event(7, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-reply-same-round-tools',
        node_id: 'ereuna_main',
        tool_call_id: 'call-time',
        tool_name: 'mcp_time_current_time',
        status: 'success',
        result: { time: '2026-06-02 16:34:00' },
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-reply-same-round-tools');
    expect(messages).toHaveLength(2);
    const replyCard = messages[1];
    expect(replyCard).toMatchObject({ role: 'assistant', status: 'running_tools', completionType: 'reply' });
    expect(replyCard.blocks.at(-1)).toMatchObject({ kind: 'text', text: '我先回复。', delivery: 'intermediate' });

    const toolBlock = replyCard.blocks.find((block) => block.kind === 'tool');
    const tools = toolBlock?.kind === 'tool' ? selectToolExecutions(state, toolBlock.toolIds) : [];
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({ id: 'call-time', name: 'mcp_time_current_time', status: 'success' });
    expect(Object.values(state.toolExecutionsById).some((tool) => tool.id === 'call-reply' && tool.hidden)).toBe(true);
  });

  it('starts a fresh assistant card when a new round begins after an intermediate reply', () => {
    // [2026-06-02] Why: reply() ends the visible content for its LLM round, but
    // the task can continue with another LLM request after reply returns ok. How:
    // replay a reply round followed by a normal tool round and a final outbound
    // finish. Purpose: the reply text remains the last visible element of its card,
    // while later thinking and tool calls move to the next work card.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-reply-break', text: 'work in phases' }),
      event(2, 'task_created', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
      }),
      event(3, 'stream_delta', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        type: 'thinking',
        content: 'round 1 thinking',
      }),
      event(4, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        tool_call_id: 'call-reply',
        tool_name: 'reply',
        arguments: { text: 'round 1 visible reply' },
      }),
      event(5, 'intermediate_reply', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        text: 'round 1 visible reply',
      }),
      event(6, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        tool_call_id: 'call-reply',
        tool_name: 'reply',
        status: 'success',
        raw_inline: 'ok',
      }),
      event(7, 'stream_delta', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        type: 'thinking',
        content: 'round 2 thinking',
      }),
      event(8, 'stream_delta', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        type: 'thinking',
        content: ' continued',
      }),
      event(9, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        tool_call_id: 'call-search',
        tool_name: 'search_in_files',
        arguments: { query: 'needle' },
      }),
      event(10, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        tool_call_id: 'call-search',
        tool_name: 'search_in_files',
        status: 'success',
        result: { success: true, data: { results: [], count: 0 } },
        raw_inline: 'no matches',
        format: 'json',
      }),
      event(11, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        tool_call_id: 'call-finish',
        tool_name: 'finish',
        arguments: { text: 'final answer' },
      }),
      event(12, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        tool_call_id: 'call-finish',
        tool_name: 'finish',
        status: 'success',
        raw_inline: 'ok',
      }),
      event(13, 'outbound_message', {
        source_inbound_seq: 1,
        task_id: 'task-reply-break',
        node_id: 'ereuna_main',
        action_type: 'finish',
        text: 'final answer',
        attachments: [],
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-reply-break');
    expect(messages).toHaveLength(4);

    const replyCard = messages[1];
    const workCard = messages[2];
    const finalCard = messages[3];
    expect(replyCard).toMatchObject({ role: 'assistant', status: 'completed', completionType: 'reply' });
    expect(replyCard.blocks.at(-1)).toMatchObject({ kind: 'text', text: 'round 1 visible reply', delivery: 'intermediate' });
    expect(workCard).toMatchObject({ role: 'assistant', status: 'completed' });
    expect(workCard.completionType).toBeUndefined();
    // [2026-06-02] Why: the fresh post-reply turn key must be stable after it is
    // assigned. How: two consecutive thinking deltas after reply should merge into
    // the same block on the same work card. Purpose: prevent every stream_delta from
    // creating its own card when one task continues after reply().
    expect(workCard.blocks[0]).toMatchObject({
      kind: 'thinking',
      text: 'round 2 thinking continued',
      streaming: false,
      endedAt: '2026-05-31T02:43:13.000Z',
    });

    const toolBlock = workCard.blocks.find((block) => block.kind === 'tool');
    const tools = toolBlock?.kind === 'tool' ? selectToolExecutions(state, toolBlock.toolIds) : [];
    expect(tools.some((tool) => tool.name === 'search_in_files' && tool.status === 'success')).toBe(true);
    expect(tools.some((tool) => tool.name === 'finish')).toBe(false);
    expect(Object.values(state.toolExecutionsById).some((tool) => tool.name === 'finish' && tool.hidden)).toBe(true);
    expect(finalCard).toMatchObject({ role: 'assistant', status: 'completed', completionType: 'finish' });
    expect(finalCard.blocks).toHaveLength(1);
    expect(finalCard.blocks[0]).toMatchObject({ kind: 'text', text: 'final answer', delivery: 'final' });
  });

  it('keeps rejected control tools visible during stream replay', () => {
    // Why: successful finish and reply calls are hidden as control bookkeeping, but a
    // rejected finish is the only visible explanation for why the stream did not
    // complete. How: replay a rejected finish tool result. Purpose: stream rendering
    // shows the rejection immediately instead of only after history hydration.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-rejected-finish', text: 'finish badly' }),
      event(2, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-rejected-finish',
        node_id: 'ereuna_main',
        tool_call_id: 'call-finish-rejected',
        tool_name: 'finish',
        arguments: { text: 'invalid output' },
      }),
      event(3, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-rejected-finish',
        node_id: 'ereuna_main',
        tool_call_id: 'call-finish-rejected',
        tool_name: 'finish',
        status: 'success',
        rejected: true,
        raw_inline: 'REJECTED: finish must be called alone',
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-rejected-finish');
    const toolBlock = messages[1].blocks.find((block) => block.kind === 'tool');
    const tools = toolBlock?.kind === 'tool' ? selectToolExecutions(state, toolBlock.toolIds) : [];

    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({
      id: 'call-finish-rejected',
      name: 'finish',
      status: 'error',
      rejected: true,
      hidden: false,
    });
  });

  it('starts a fresh card when streamed tool deltas begin after a reply card', () => {
    // [2026-06-03] Why: history hydration treats the first assistant row after a
    // reply as new visible work even when that row starts with tool calls rather than
    // text. How: replay a reply card followed by provider tool_call_delta events for
    // the next LLM round. Purpose: live stream cards split the same way as refreshed
    // structured history instead of appending all later work under the reply.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-reply-then-tool-delta', text: 'work in two rounds' }),
      event(2, 'task_created', {
        source_inbound_seq: 1,
        task_id: 'task-reply-then-tool-delta',
        node_id: 'ereuna_main',
      }),
      event(3, 'intermediate_reply', {
        source_inbound_seq: 1,
        task_id: 'task-reply-then-tool-delta',
        node_id: 'ereuna_main',
        text: 'first visible reply',
      }),
      event(4, 'tool_call_delta', {
        source_inbound_seq: 1,
        task_id: 'task-reply-then-tool-delta',
        node_id: 'ereuna_main',
        event: 'tool_call_start',
        index: 0,
        id: 'call-search-after-reply',
        name: 'search_in_files',
      }),
      event(5, 'tool_call_delta', {
        source_inbound_seq: 1,
        task_id: 'task-reply-then-tool-delta',
        node_id: 'ereuna_main',
        event: 'tool_call_args_delta',
        index: 0,
        id: 'call-search-after-reply',
        delta: '{"query":"needle"}',
      }),
      event(6, 'tool_call_start', {
        source_inbound_seq: 1,
        task_id: 'task-reply-then-tool-delta',
        node_id: 'ereuna_main',
        tool_call_id: 'call-search-after-reply',
        tool_name: 'search_in_files',
        arguments: { query: 'needle' },
      }),
      event(7, 'tool_call_end', {
        source_inbound_seq: 1,
        task_id: 'task-reply-then-tool-delta',
        node_id: 'ereuna_main',
        tool_call_id: 'call-search-after-reply',
        tool_name: 'search_in_files',
        status: 'success',
        result: { success: true },
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-reply-then-tool-delta');
    expect(messages).toHaveLength(3);
    expect(messages[1]).toMatchObject({ role: 'assistant', completionType: 'reply', status: 'completed' });
    expect(messages[1].blocks).toHaveLength(1);
    expect(messages[1].blocks[0]).toMatchObject({ kind: 'text', text: 'first visible reply', delivery: 'intermediate' });

    const workCard = messages[2];
    expect(workCard).toMatchObject({ role: 'assistant', status: 'running_tools' });
    expect(workCard.completionType).toBeUndefined();
    expect(workCard.blocks.map((block) => block.kind)).toEqual(['tool']);
    const toolBlock = workCard.blocks[0];
    const tools = toolBlock.kind === 'tool' ? selectToolExecutions(state, toolBlock.toolIds) : [];
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({ id: 'call-search-after-reply', name: 'search_in_files', status: 'success' });
    expect(tools[0].argumentsText).toBe('{"query":"needle"}');
  });

  it('does not merge thinking deltas across intervening text blocks', () => {
    // [2026-06-03] Why: thinking blocks must preserve render order instead of
    // reaching backward across text or tool blocks. How: replay thinking, then text,
    // then more thinking in one streamed assistant turn. Purpose: the reducer appends
    // blocks by event order and avoids moving later reasoning above visible text.
    const state = replaySupervisorEvents([
      event(1, 'inbound_message', { conversation_key: 'web:conv-thinking-order', text: 'show order' }),
      event(2, 'stream_delta', {
        source_inbound_seq: 1,
        task_id: 'task-thinking-order',
        node_id: 'ereuna_main',
        type: 'thinking',
        content: 'first thinking',
      }),
      event(3, 'stream_delta', {
        source_inbound_seq: 1,
        task_id: 'task-thinking-order',
        node_id: 'ereuna_main',
        type: 'text',
        content: 'visible text',
      }),
      event(4, 'stream_delta', {
        source_inbound_seq: 1,
        task_id: 'task-thinking-order',
        node_id: 'ereuna_main',
        type: 'thinking',
        content: 'second thinking',
      }),
    ], createInitialChatState());

    const messages = selectMessages(state, 'conv-thinking-order');
    expect(messages[1].blocks.map((block) => block.kind)).toEqual(['thinking', 'text', 'thinking']);
    expect(messages[1].blocks[0]).toMatchObject({ kind: 'thinking', text: 'first thinking' });
    expect(messages[1].blocks[1]).toMatchObject({ kind: 'text', text: 'visible text' });
    expect(messages[1].blocks[2]).toMatchObject({ kind: 'thinking', text: 'second thinking' });
  });
});
