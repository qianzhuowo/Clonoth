// [2026-05-31] Step 2A tests for the reducer-backed chat store and log-panel layout.
// Why: the new store must coexist with the legacy store while proving that WebSocket
// events flow through eventReducer and that the event log lives in the right rail.
// How: use a fake browser WebSocket plus fetch stubs for Supervisor endpoints, then
// assert selector-derived messages, startup hydration, and rendered log rows. Purpose:
// protect the refactor boundary before later components switch to the new message model.
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EventLogPanel } from '../components/log/EventLogPanel';
import { AppLayout } from '../components/layout/AppLayout';
import { selectMessages } from '../store/eventSelectors';
import { useChatStoreV2 } from '../store/chatStoreV2';
import type { SupervisorEvent } from '../types/chat';
import type { WsMessage } from '../types/message';

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

function supervisorEvent(seq: number, type: string, payload: Record<string, unknown>, sessionId = 'sess-v2'): SupervisorEvent {
  return {
    seq,
    event_id: `v2-ev-${seq}`,
    ts: `2026-05-31T03:10:${String(seq).padStart(2, '0')}.000Z`,
    session_id: sessionId,
    type,
    payload,
  };
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('chatStoreV2', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
    useChatStoreV2.getState().resetState();
  });

  afterEach(() => {
    useChatStoreV2.getState().resetState();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('routes WebSocket events through eventReducer without directly appending the sent user message', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/v1/inbound')) {
        return jsonResponse({ session_id: 'sess-v2', inbound_seq: 1, accepted: true });
      }
      return jsonResponse([]);
    }));

    const conversationId = useChatStoreV2.getState().createConversation();
    await useChatStoreV2.getState().sendMessage('run uname', undefined, 'ereuna_main');

    expect(selectMessages(useChatStoreV2.getState(), conversationId)).toHaveLength(0);
    expect(FakeWebSocket.instances).toHaveLength(1);
    const ws = FakeWebSocket.instances[0];
    expect(ws.url).toContain('/v1/sessions/sess-v2/ws');

    ws.open();
    expect(ws.sent).toEqual([JSON.stringify({ last_seq: 0 })]);
    // [2026-06-01] Opening the browser WebSocket is enough to mark realtime as healthy.
    // Why: waiting for a non-ping event leaves the right rail stuck in a false
    // connecting or disconnected state when the socket is already usable. How: the
    // onOpen callback updates connectionStatus immediately. Purpose: the UI reflects
    // the actual connection before the first Supervisor event arrives.
    expect(useChatStoreV2.getState().connectionStatus).toBe('open');

    ws.receive(supervisorEvent(1, 'inbound_message', { conversation_key: `web:${conversationId}`, text: 'run uname' }));
    ws.receive(supervisorEvent(2, 'stream_delta', { source_inbound_seq: 1, type: 'text', content: 'working' }));
    ws.receive(supervisorEvent(3, 'outbound_message', { source_inbound_seq: 1, text: 'done', attachments: [] }));
    ws.receive(supervisorEvent(4, 'task_completed', { source_inbound_seq: 1, status: 'completed' }));

    const state = useChatStoreV2.getState();
    const messages = selectMessages(state, conversationId);
    expect(messages.map((message) => message.role)).toEqual(['user', 'assistant', 'assistant']);
    expect(messages[0].blocks[0]).toMatchObject({ kind: 'text', text: 'run uname' });
    // Why: outbound_message is rendered as a separate assistant card. How: the first
    // assistant keeps the closed stream preview and the second assistant holds the final
    // outbound text. Purpose: this store-level test follows reducer ownership exactly.
    expect(messages[1].status).toBe('completed');
    expect(messages[1].blocks[0]).toMatchObject({ kind: 'text', text: 'working', delivery: 'stream', streaming: false });
    expect(messages[2].status).toBe('completed');
    expect(messages[2].blocks[0]).toMatchObject({ kind: 'text', text: 'done', delivery: 'final', streaming: false });
    expect(state.lastSeqBySession['sess-v2']).toBe(4);
    expect(state.isGenerating).toBe(false);
    // [2026-06-01] A terminal task event is normal cleanup, not a broken socket.
    // Why: the right panel should not show a red disconnected warning after a
    // successful assistant turn. How: completion closes realtime transport and moves
    // the status back to idle. Purpose: idle means no active socket is needed.
    expect(state.connectionStatus).toBe('idle');
    expect(state.eventLog.map((entry) => entry.type)).toEqual(['inbound_message', 'stream_delta', 'outbound_message', 'task_completed']);
  });

  it('returns to idle after cancelling an active task', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ok: true })));

    useChatStoreV2.setState({
      conversations: [{ id: 'conv-cancel', sessionId: 'sess-cancel', title: 'Cancel test', updatedAt: '2026-05-31T03:30:00.000Z' }],
      activeConversationId: 'conv-cancel',
      isGenerating: true,
      connectionStatus: 'open',
    });

    await useChatStoreV2.getState().cancelCurrentTask();

    // [2026-06-01] User cancellation is also an intentional local stop.
    // Why: treating cancel as closed produces the same false disconnected warning as
    // task completion. How: cancel clears generation and returns the connection state
    // to idle. Purpose: only unexpected disconnects should remain red.
    expect(useChatStoreV2.getState()).toMatchObject({ isGenerating: false, connectionStatus: 'idle' });
  });

  it('loads server sessions and keeps a historical finish call separate from prior tool-only work', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions?channel=web')) {
        return jsonResponse([{ session_id: 'sess-history', conversation_key: 'web:conv-history', channel: 'web', created_at: '2026-05-31T03:00:00.000Z', updated_at: '2026-05-31T03:01:00.000Z' }]);
      }
      if (url.includes('/v1/sessions/sess-history/history')) {
        return jsonResponse([
          { id: 'u1', role: 'user', content: 'hello', message_type: 'user_input', created_at: '2026-05-31T03:00:01.000Z' },
          { id: 'a-tools', role: 'assistant', content: '', created_at: '2026-05-31T03:00:02.000Z', thinking: 'thought', tool_calls: [{ id: 'search-1', name: 'search_in_files', arguments: { query: 'needle' } }] },
          { id: 't-tools', role: 'tool', content: 'found one', message_type: 'tool_result', tool_call_id: 'search-1', tool_name: 'search_in_files', created_at: '2026-05-31T03:00:03.000Z' },
          { id: 'a-finish', role: 'assistant', content: '', created_at: '2026-05-31T03:00:04.000Z', tool_calls: [{ id: 'finish-1', name: 'finish', arguments: { text: 'hi there' } }] },
          { id: 't-finish', role: 'tool', content: 'ok', message_type: 'tool_result', tool_call_id: 'finish-1', tool_name: 'finish', created_at: '2026-05-31T03:00:05.000Z' },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStoreV2.getState().loadStartup();

    await waitFor(() => expect(useChatStoreV2.getState().conversations).toHaveLength(1));
    await waitFor(() => expect(selectMessages(useChatStoreV2.getState(), 'conv-history')).toHaveLength(3));

    const state = useChatStoreV2.getState();
    const messages = selectMessages(state, 'conv-history');
    expect(state.activeConversationId).toBe('conv-history');
    expect(messages[0].blocks[0]).toMatchObject({ kind: 'text', text: 'hello', delivery: 'history' });
    // Why: finish is persisted as its own assistant tool call by the backend guard.
    // How: this history fixture places a visible finish after a tool-only assistant row.
    // Purpose: hydration must render the prior tool card and the final finish card separately.
    expect(messages[1].blocks.map((block) => block.kind)).toEqual(['thinking', 'tool']);
    expect(messages[2].blocks.map((block) => block.kind)).toEqual(['tool', 'text']);
    expect(messages[2].blocks.at(-1)).toMatchObject({ kind: 'text', text: 'hi there', delivery: 'history' });
  });

  it('does not create sidebar conversations for events from unknown branch sessions', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/v1/inbound')) {
        return jsonResponse({ session_id: 'sess-v2', inbound_seq: 1, accepted: true });
      }
      return jsonResponse([]);
    }));

    const conversationId = useChatStoreV2.getState().createConversation();
    await useChatStoreV2.getState().sendMessage('start work', undefined, 'ereuna_main');
    expect(useChatStoreV2.getState().conversations.map((conversation) => conversation.id)).toEqual([conversationId]);

    // Why: child agents and branch sessions may stream events on session ids that the
    // web sidebar did not create. How: replay one event that resolves to its own session
    // and one that resolves to the parent turn through source_inbound_seq. Purpose: the
    // sidebar neither gains a child conversation nor replaces the parent session id.
    FakeWebSocket.instances[0].receive(supervisorEvent(2, 'stream_delta', { type: 'text', content: 'branch work' }, 'branch-sess'));
    FakeWebSocket.instances[0].receive(supervisorEvent(3, 'stream_delta', { source_inbound_seq: 1, type: 'text', content: 'parent work' }, 'child-sess'));

    expect(useChatStoreV2.getState().conversations.map((conversation) => conversation.id)).toEqual([conversationId]);
    expect(useChatStoreV2.getState().conversations[0].sessionId).toBe('sess-v2');
  });

  it('preserves live messages when history loading races with an active generation', async () => {
    const liveMessage: WsMessage = {
      id: 'message:conv-race:live-assistant',
      conversationId: 'conv-race',
      sessionId: 'sess-race',
      role: 'assistant',
      status: 'streaming',
      createdAt: '2026-05-31T03:05:00.000Z',
      updatedAt: '2026-05-31T03:05:01.000Z',
      source: { inboundSeq: 1 },
      blocks: [{
        id: 'message:conv-race:live-assistant|block:text:live',
        kind: 'text',
        text: 'live stream',
        delivery: 'stream',
        streaming: true,
        createdAt: '2026-05-31T03:05:00.000Z',
        updatedAt: '2026-05-31T03:05:01.000Z',
        eventIds: ['live-ev-1'],
      }],
      eventIds: ['live-ev-1'],
    };

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/v1/sessions/sess-race/history')) {
        return jsonResponse([
          { id: 'old-user', role: 'user', content: 'old message', message_type: 'user_input', created_at: '2026-05-31T03:00:00.000Z' },
        ]);
      }
      return jsonResponse([]);
    }));

    useChatStoreV2.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-race', sessionId: 'sess-race', title: 'Race test', updatedAt: '2026-05-31T03:05:00.000Z' }],
      activeConversationId: 'conv-race',
      isGenerating: true,
      messagesById: { ...state.messagesById, [liveMessage.id]: liveMessage },
      messageOrderByConversation: { ...state.messageOrderByConversation, 'conv-race': [liveMessage.id] },
      conversationIdsBySession: { ...state.conversationIdsBySession, 'sess-race': 'conv-race' },
    }));

    useChatStoreV2.getState().selectConversation('conv-race');

    // Why: selecting a generating conversation can finish history loading after
    // WebSocket stream blocks already exist. How: this assertion requires hydration
    // to keep the live message while adding history. Purpose: avoid losing active
    // output when an older history response arrives late.
    await waitFor(() => expect(selectMessages(useChatStoreV2.getState(), 'conv-race')).toHaveLength(2));
    expect(useChatStoreV2.getState().messagesById[liveMessage.id]).toBeDefined();
    expect(selectMessages(useChatStoreV2.getState(), 'conv-race').some((message) => message.id === liveMessage.id)).toBe(true);
  });
});

describe('Step 2A layout and event log panel', () => {
  beforeEach(() => {
    useChatStoreV2.getState().resetState();
  });

  afterEach(() => {
    useChatStoreV2.getState().resetState();
  });

  it('keeps AppLayout compatible while rendering the optional log slot in the right rail', () => {
    render(
      <AppLayout
        composer={<div>composer</div>}
        header={<div>header</div>}
        logPanel={<div>log body</div>}
        sidebar={<div>sidebar</div>}
      >
        <div>chat body</div>
      </AppLayout>,
    );

    expect(screen.getByText('chat body')).toBeInTheDocument();
    expect(screen.getByText('log body')).toBeInTheDocument();
    // Why: the event log moved from a bottom strip into the desktop right rail.
    // How: AppLayout exposes the rail with an accessible label instead of the old
    // placeholder. Purpose: tests protect the layout contract that carries EventLogPanel.
    expect(screen.getByLabelText('事件日志面板')).toBeInTheDocument();
    expect(screen.queryByLabelText('状态面板占位')).not.toBeInTheDocument();
  });

  it('renders the active session event log as timestamped monospace rows', () => {
    useChatStoreV2.setState((state) => ({
      ...state,
      conversations: [{ id: 'conv-log', sessionId: 'sess-log', title: 'Log test', updatedAt: '2026-05-31T03:00:00.000Z' }],
      activeConversationId: 'conv-log',
      eventLog: [
        {
          id: 'log-1',
          eventId: 'event-1',
          seq: 1,
          ts: '2026-05-31T03:20:30.000Z',
          sessionId: 'sess-log',
          conversationId: 'conv-log',
          type: 'stream_delta',
          payload: { content: 'hello' },
          summary: 'hello',
        },
      ],
    }));

    render(<EventLogPanel />);

    expect(screen.getByText(/\[stream_delta\]/)).toBeInTheDocument();
    expect(screen.getByText(/hello/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /折叠|展开/ })).not.toBeInTheDocument();
  });
});
