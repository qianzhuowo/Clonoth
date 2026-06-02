// [2026-05-31] MessageListV2 renders only normalized WsMessage objects.
// Why: the new message model carries its own streaming status, so the list no longer
// needs a separate StreamPreview or isTyping prop. How: map messages to MessageCard and
// scroll after message, block, or tool state changes. Purpose: prepare the frontend to
// replace the old MessageList plus StreamPreview composition in a later step.
import { useEffect, useMemo, useRef } from 'react';

import type { ToolExecution, WsMessage } from '../../../types/message';
import { MessageCard } from './MessageCard';

interface MessageListV2Props {
  messages: WsMessage[];
  toolsById: Record<string, ToolExecution>;
}

function getScrollSignature(messages: WsMessage[], toolsById: Record<string, ToolExecution>): string {
  const messagePart = messages.map((message) => `${message.id}:${message.updatedAt}:${message.status}:${message.blocks.length}`).join('|');
  const toolPart = Object.values(toolsById).map((tool) => `${tool.stableId}:${tool.updatedAt}:${tool.status}`).join('|');
  return `${messagePart}::${toolPart}`;
}

export const MessageListV2 = ({ messages, toolsById }: MessageListV2Props) => {
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollSignature = useMemo(() => getScrollSignature(messages, toolsById), [messages, toolsById]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [scrollSignature]);

  if (messages.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-sm text-[var(--duties-tertiary)]">
        请选择或创建一个对话。
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl">
        {messages.map((message) => (
          <MessageCard key={message.id} message={message} toolsById={toolsById} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
};

export type { MessageListV2Props };
