// [2026-05-31] Tests for the v2 message rendering layer.
// Why: Step 2B introduces new components without wiring them into the app yet, so
// behavior must be verified directly. How: render normalized WsMessage fixtures and
// assert the visible contract for streaming text, hidden tools, and list scrolling.
// Purpose: keep the new unified MessageCard safe to adopt in a later integration step.
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { MessageCard, MessageListV2, ToolCallCard } from '../components/chat/v2';
import type { ToolExecution, WsMessage } from '../types/message';

const now = '2026-05-31T03:10:00.000Z';

function baseMessage(overrides: Partial<WsMessage> = {}): WsMessage {
  return {
    id: 'msg-1',
    conversationId: 'conv-1',
    sessionId: 'sess-1',
    role: 'assistant',
    status: 'streaming',
    createdAt: now,
    updatedAt: now,
    source: { taskId: 'task-1', nodeId: 'node-1', nodeName: 'Node One' },
    blocks: [],
    eventIds: ['ev-1'],
    ...overrides,
  };
}

function baseTool(overrides: Partial<ToolExecution> = {}): ToolExecution {
  return {
    stableId: 'tool-1',
    messageId: 'msg-1',
    blockId: 'block-tool',
    id: 'call-1',
    name: 'execute_command',
    status: 'running',
    arguments: { command: 'pwd' },
    argumentsText: '{"command":"pwd"}',
    summary: 'running command',
    createdAt: now,
    updatedAt: now,
    eventIds: ['ev-tool'],
    ...overrides,
  };
}

describe('MessageCard v2', () => {
  it('renders streaming markdown text with a cursor and status indicator', () => {
    render(
      <MessageCard
        message={baseMessage({
          blocks: [{
            id: 'block-text',
            kind: 'text',
            text: '**Hello** world',
            delivery: 'stream',
            streaming: true,
            createdAt: now,
            updatedAt: now,
            eventIds: ['ev-text'],
          }],
        })}
        toolsById={{}}
      />,
    );

    expect(screen.getByText('助手')).toBeInTheDocument();
    expect(screen.getByText('输出中')).toBeInTheDocument();
    expect(screen.getByText('Hello')).toBeInTheDocument();
    // [2026-06-01] Why: the streaming cursor is now a styled element instead of a
    // literal block character. How: assert its accessible label. Purpose: the test
    // follows the rendered UI contract without depending on decorative text.
    expect(screen.getByLabelText('流式输出光标')).toBeInTheDocument();
  });

  it('renders visible tools but omits hidden successful control tools', () => {
    const visibleTool = baseTool();
    const hiddenTool = baseTool({
      stableId: 'tool-hidden',
      id: 'call-finish',
      name: 'finish',
      status: 'success',
      hidden: true,
      summary: 'done',
    });

    render(
      <MessageCard
        message={baseMessage({
          status: 'running_tools',
          blocks: [{
            id: 'block-tool',
            kind: 'tool',
            toolIds: ['tool-1', 'tool-hidden'],
            createdAt: now,
            updatedAt: now,
            eventIds: ['ev-tool-block'],
          }],
        })}
        toolsById={{ 'tool-1': visibleTool, 'tool-hidden': hiddenTool }}
      />,
    );

    expect(screen.getByText('execute_command')).toBeInTheDocument();
    expect(screen.queryByText('finish')).not.toBeInTheDocument();
  });

  it('previews oversized tool details and shows byte sizes', () => {
    const longArguments = 'a'.repeat(10020);
    const longResult = 'b'.repeat(10030);

    render(
      <MessageCard
        message={baseMessage({
          status: 'completed',
          blocks: [{
            id: 'block-tool-large',
            kind: 'tool',
            toolIds: ['tool-large'],
            createdAt: now,
            updatedAt: now,
            eventIds: ['ev-tool-large'],
          }],
        })}
        toolsById={{
          'tool-large': baseTool({
            stableId: 'tool-large',
            blockId: 'block-tool-large',
            status: 'success',
            argumentsText: longArguments,
            rawInline: longResult,
            summary: '',
          }),
        }}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /execute_command/ }));

    // Why: tool payloads can be very large. How: the expanded details should show a
    // 10000-character preview, byte-size labels, and an opt-in full-content button.
    // Purpose: keep the chat timeline responsive without permanently truncating data.
    expect(screen.getByText('参数')).toBeInTheDocument();
    expect(screen.getByText('结果')).toBeInTheDocument();
    expect(screen.getAllByText(/\[\d+\.\d KB\]/)).toHaveLength(2);
    expect(screen.queryByText(longArguments)).not.toBeInTheDocument();
    expect(screen.queryByText(longResult)).not.toBeInTheDocument();

    const fullContentButtons = screen.getAllByRole('button', { name: '查看完整内容' });
    expect(fullContentButtons).toHaveLength(2);
    fireEvent.click(fullContentButtons[0]);
    expect(screen.getByText(longArguments)).toBeInTheDocument();
  });

  it('renders unified execute_command data before legacy inline text', () => {
    render(
      <ToolCallCard
        tool={baseTool({
          status: 'success',
          format: 'text',
          summary: '',
          rawInline: 'returncode=1\nlegacy output\n',
          result: {
            ok: true,
            data: {
              result: 'unified readable command result',
              returncode: 0,
              output: 'unified output\n',
            },
            error: null,
          },
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /execute_command/ }));

    // Why: tools now return a unified envelope with structured fields under data.
    // How: the card should render data.returncode and data.output before raw_inline.
    // Purpose: new backend results stay readable while old raw text remains fallback only.
    expect(screen.getByText('返回码=0')).toBeInTheDocument();
    expect(screen.getByText('unified output')).toBeInTheDocument();
    expect(screen.queryByText(/legacy output/)).not.toBeInTheDocument();
  });

  it('renders unified read_file sections from data.results', () => {
    render(
      <ToolCallCard
        tool={baseTool({
          name: 'read_file',
          status: 'success',
          format: 'text',
          summary: '',
          rawInline: '── old.ts ──\nold content',
          result: {
            ok: true,
            data: {
              result: 'read src/new.ts',
              results: [{ path: 'src/new.ts', type: 'text', content: 'const answer = 42;' }],
            },
            error: null,
          },
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /read_file/ }));

    // Why: read_file now sends structured file entries inside data.results. How: render
    // each entry as a named file section before parsing legacy transcript headers. Purpose:
    // users can inspect files even when raw_inline is absent or stale.
    expect(screen.getByText('src/new.ts')).toBeInTheDocument();
    expect(screen.getByText('const answer = 42;')).toBeInTheDocument();
    expect(screen.queryByText(/old content/)).not.toBeInTheDocument();
  });

  it('renders unified JSON tool structures from data fields', () => {
    render(
      <ToolCallCard
        tool={baseTool({
          name: 'search_in_files',
          status: 'success',
          format: 'json',
          summary: '',
          result: {
            ok: true,
            data: {
              result: '1 result found',
              results: [{ file: 'src/example.ts', line: 7, match: 'needle', context: 'const needle = true;' }],
              count: 1,
              truncated: false,
            },
            error: null,
          },
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /search_in_files/ }));

    // Why: search_in_files structure moved under result.data. How: read rows, count,
    // and truncation from data first. Purpose: the structured renderer avoids a raw
    // JSON dump for current backend responses.
    expect(screen.getByText('找到 1 个结果')).toBeInTheDocument();
    expect(screen.getByText('src/example.ts:7')).toBeInTheDocument();
    expect(screen.getByText('needle')).toBeInTheDocument();
  });

  it('uses data.result as the generic fallback for unknown tools', () => {
    render(
      <ToolCallCard
        tool={baseTool({
          name: 'custom_tool',
          status: 'success',
          format: 'json',
          summary: '',
          result: {
            ok: true,
            data: {
              result: 'plain readable fallback',
              extra: { hiddenInFallback: true },
            },
            error: null,
          },
        })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /custom_tool/ }));

    // Why: unknown tools should prefer the human-readable data.result string. How: render
    // it as plain preformatted text before falling back to highlighted JSON. Purpose:
    // users see the backend-provided summary instead of an envelope dump.
    expect(screen.getByText('plain readable fallback')).toBeInTheDocument();
    expect(screen.queryByText(/hiddenInFallback/)).not.toBeInTheDocument();
  });

  it('renders the new list and scrolls to the bottom when messages change', () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;

    const { rerender } = render(<MessageListV2 messages={[baseMessage({ id: 'msg-1' })]} toolsById={{}} />);
    rerender(<MessageListV2 messages={[baseMessage({ id: 'msg-1' }), baseMessage({ id: 'msg-2' })]} toolsById={{}} />);

    expect(scrollIntoView).toHaveBeenCalled();
  });
});
