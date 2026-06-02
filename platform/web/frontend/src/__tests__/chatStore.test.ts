// [2026-05-16] Updated: tests for new store without mock data.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { buildChatMessagesFromHistory, useChatStore } from '../store/chatStore';
import type { StructuredMessage } from '../api/supervisorClient';

// [2026-05-17] These helpers model the browser WebSocket contract so the realtime
// store path can be tested without a live Supervisor process. They keep the test
// focused on why Phase 3 exists: replacing event polling with catch-up plus push
// events while still preserving tool-call metadata on rendered messages.
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  constructor(public readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.onclose?.();
  }

  open() {
    this.onopen?.();
  }

  receive(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent<string>);
  }
}

function makeSupervisorEvent(seq: number, type: string, payload: Record<string, unknown>) {
  // [2026-05-17] The helper builds the same event shape emitted by Supervisor so
  // realtime tests exercise chatStore exactly as production WebSocket messages do.
  return {
    seq,
    event_id: `ev-${seq}`,
    ts: '2026-05-17T07:35:00.000Z',
    session_id: 'sess-rt',
    type,
    payload,
  };
}

function installRealtimeHarness() {
  // [2026-05-17] The fetch stub only handles POST /v1/inbound because the new
  // realtime path must not depend on /events polling to complete a user turn.
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    session_id: 'sess-rt',
    inbound_seq: 1,
    accepted: true,
  }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })));
}

describe('chat store', () => {
  beforeEach(() => {
    useChatStore.getState().resetState();
  });

  afterEach(() => {
    // [2026-05-17] Reset store and browser globals after realtime tests so fake
    // WebSocket instances and fetch responses cannot leak into unrelated tests.
    useChatStore.getState().resetState();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('starts with empty conversations', () => {
    const state = useChatStore.getState();
    expect(state.conversations).toHaveLength(0);
    expect(state.activeConversationId).toBeNull();
  });

  it('creates a new conversation and selects it', () => {
    useChatStore.getState().createConversation();
    const state = useChatStore.getState();
    expect(state.conversations).toHaveLength(1);
    expect(state.activeConversation?.title).toBe('新对话');
    expect(state.activeConversation?.messages).toHaveLength(0);
  });

  it('deletes a conversation', () => {
    const id = useChatStore.getState().createConversation();
    expect(useChatStore.getState().conversations).toHaveLength(1);
    useChatStore.getState().deleteConversation(id);
    expect(useChatStore.getState().conversations).toHaveLength(0);
  });

  it('builds display messages from flat Clonoth history with paired tool results', () => {
    // This fixture mirrors the production history shape. It protects the rewrite from
    // regressing into the old approach where role=tool rows leaked into chat content.
    const history: StructuredMessage[] = [
      {
        id: 'u1',
        role: 'user',
        content: '你是谁喵',
        message_type: 'user_input',
        created_at: '2026-05-17T01:44:38.440636+00:00',
        source_node_id: 'claudecatgirl',
      },
      {
        id: 'a1',
        role: 'assistant',
        content: '',
        message_type: 'assistant',
        created_at: '2026-05-17T01:44:47.164886+00:00',
        source_node_id: 'claudecatgirl',
        thinking: 'thinking one',
        tool_calls: [{ id: 'jt_finish_ok', name: 'finish', arguments: { text: '本喵是你的专属猫娘呀喵～' } }],
      },
      {
        id: 't1',
        role: 'tool',
        content: 'ok',
        message_type: 'tool_result',
        created_at: '2026-05-17T01:44:47.168266+00:00',
        source_node_id: 'claudecatgirl',
        tool_call_id: 'jt_finish_ok',
        tool_name: 'finish',
      },
      {
        id: 'u2',
        role: 'user',
        content: 'hidden summary',
        message_type: 'summary',
        created_at: '2026-05-17T01:44:50.000000+00:00',
      },
      {
        id: 'u3',
        role: 'user',
        content: '你跑在哪里喵',
        message_type: 'user_input',
        created_at: '2026-05-17T01:44:56.685606+00:00',
        source_node_id: 'ereuna_main',
      },
      {
        id: 'a2',
        role: 'assistant',
        content: '',
        message_type: 'assistant',
        created_at: '2026-05-17T01:45:07.068396+00:00',
        source_node_id: 'ereuna_main',
        thinking: 'thinking rejected',
        tool_calls: [{ id: 'jt_finish_bad', name: 'finish', arguments: { text: '跑在云端喵～' } }],
      },
      {
        id: 't2',
        role: 'tool',
        content: '❌ REJECTED: finish needs a relevant tool first.',
        message_type: 'tool_result',
        created_at: '2026-05-17T01:45:07.068725+00:00',
        source_node_id: 'ereuna_main',
        tool_call_id: 'jt_finish_bad',
        tool_name: 'finish',
      },
      {
        id: 'a3',
        role: 'assistant',
        content: '',
        message_type: 'assistant',
        created_at: '2026-05-17T01:45:23.096001+00:00',
        source_node_id: 'ereuna_main',
        thinking: 'thinking command',
        tool_calls: [{ id: 'jt_cmd', name: 'execute_command', arguments: { command: 'uname -s' } }],
      },
      {
        id: 't3',
        role: 'tool',
        content: 'returncode=0\nLinux\n',
        message_type: 'tool_result',
        created_at: '2026-05-17T01:45:25.695133+00:00',
        source_node_id: 'ereuna_main',
        tool_call_id: 'jt_cmd',
        tool_name: 'execute_command',
      },
      {
        id: 'a4',
        role: 'assistant',
        content: '',
        message_type: 'assistant',
        created_at: '2026-05-17T01:45:31.049966+00:00',
        source_node_id: 'ereuna_main',
        thinking: 'thinking finish',
        tool_calls: [{ id: 'jt_finish_final', name: 'finish', arguments: { text: '跑在一台 Linux 服务器上喵～' } }],
      },
      {
        id: 't4',
        role: 'tool',
        content: 'ok',
        message_type: 'tool_result',
        created_at: '2026-05-17T01:45:31.052930+00:00',
        source_node_id: 'ereuna_main',
        tool_call_id: 'jt_finish_final',
        tool_name: 'finish',
      },
    ];

    const messages = buildChatMessagesFromHistory(history, 'conv-history');

    expect(messages.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant', 'assistant']);
    expect(messages[1].content).toBe('本喵是你的专属猫娘呀喵～');
    expect(messages[1].toolCalls?.[0]).toMatchObject({ name: 'finish', status: 'success', isAutoResult: true });
    expect(messages[1].toolCalls?.[0].result).toBeUndefined();

    expect(messages[3].content).toBe('跑在云端喵～');
    expect(messages[3].toolCalls?.[0]).toMatchObject({ name: 'finish', status: 'error', rejected: true });
    expect(messages[3].toolCalls?.[0].result).toContain('❌ REJECTED: finish needs a relevant tool first.');

    expect(messages[4].content).toBe('跑在一台 Linux 服务器上喵～');
    expect(messages[4].thinking).toContain('thinking command');
    expect(messages[4].thinking).toContain('thinking finish');
    expect(messages[4].toolCalls?.map((tc) => tc.name)).toEqual(['execute_command', 'finish']);
    expect(messages[4].toolCalls?.[0]).toMatchObject({ status: 'success', result: 'returncode=0 Linux' });
    expect(messages[4].toolCalls?.[1]).toMatchObject({ status: 'success', isAutoResult: true });
    expect(messages.some((m) => m.content.includes('returncode=0'))).toBe(false);
    expect(messages.some((m) => m.content.includes('hidden summary'))).toBe(false);
  });

  it('uses WebSocket realtime events and attaches tool calls to the final assistant message', async () => {
    installRealtimeHarness();

    await useChatStore.getState().sendActiveMessage('run uname');

    expect(FakeWebSocket.instances).toHaveLength(1);
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toContain('/v1/sessions/sess-rt/ws');

    ws.open();
    expect(ws.sent).toEqual([JSON.stringify({ last_seq: 0 })]);

    ws.receive({ type: 'ping' });
    ws.receive(makeSupervisorEvent(1, 'tool_call_start', {
      tool_call_id: 'call-1',
      tool_name: 'execute_command',
      arguments: { command: 'uname -s' },
      node_id: 'ereuna_main',
    }));
    ws.receive(makeSupervisorEvent(2, 'tool_call_end', {
      tool_call_id: 'call-1',
      status: 'success',
      summary: 'returncode=0 Linux',
    }));
    ws.receive(makeSupervisorEvent(3, 'outbound_message', {
      text: 'done',
      attachments: [],
    }));

    const state = useChatStore.getState();
    const messages = state.activeConversation?.messages ?? [];
    expect(state.lastEventSeq['sess-rt']).toBe(3);
    expect(state.streamPreview).toMatchObject({ isActive: false, progressLines: [] });
    expect(messages.map((message) => message.role)).toEqual(['user', 'assistant']);
    expect(messages[1].content).toBe('done');
    expect(messages[1].toolCalls?.[0]).toMatchObject({
      id: 'call-1',
      name: 'execute_command',
      summary: 'returncode=0 Linux',
      arguments: { command: 'uname -s' },
      nodeId: 'ereuna_main',
      status: 'success',
    });
  });

  it('attaches tool calls to intermediate replies and clears them before the final message', async () => {
    installRealtimeHarness();

    await useChatStore.getState().sendActiveMessage('send intermediate');
    const ws = FakeWebSocket.instances[0];
    ws.open();

    ws.receive(makeSupervisorEvent(1, 'stream_delta', { type: 'thinking', content: 'thinking before reply' }));
    ws.receive(makeSupervisorEvent(2, 'tool_call_start', {
      tool_call_id: 'call-2',
      tool_name: 'reply',
      arguments: { text: 'partial' },
      node_id: 'ereuna_main',
    }));
    ws.receive(makeSupervisorEvent(3, 'tool_call_end', {
      tool_call_id: 'call-2',
      status: 'success',
      summary: '',
    }));
    ws.receive(makeSupervisorEvent(4, 'intermediate_reply', { text: 'partial' }));
    ws.receive(makeSupervisorEvent(5, 'outbound_message', { text: 'final', attachments: [] }));

    const messages = useChatStore.getState().activeConversation?.messages ?? [];
    expect(messages.map((message) => message.content)).toEqual(['send intermediate', 'partial', 'final']);
    expect(messages[1]).toMatchObject({ isIntermediate: true, thinking: 'thinking before reply' });
    expect(messages[1].toolCalls?.[0]).toMatchObject({ id: 'call-2', name: 'reply', status: 'success' });
    expect(messages[2].thinking).toBe('thinking before reply');
    expect(messages[2].toolCalls).toBeUndefined();
  });
});
