// [2026-05-31] Selector hook for the reducer-backed chat store.
// Why: components should not read the normalized reducer tables directly. How: derive
// the active conversation and ordered WsMessage list from chatStoreV2 with selectors.
// Purpose: later UI migration can swap useChat to this hook without changing store internals.
// [2026-05-31] Step 3 splits metadata and message subscriptions. Why: selectMessages
// creates a fresh array, which makes React's external-store snapshot unstable if it is
// nested inside the main object selector. How: subscribe to messages separately with
// useShallow so equal message arrays reuse their previous reference. Purpose: prevent
// infinite rerender loops when App mounts the V2 hook.
import { useShallow } from 'zustand/react/shallow';

import { selectMessages } from '../store/eventSelectors';
import { useChatStoreV2 } from '../store/chatStoreV2';
import type { WsMessage } from '../types/message';

const EMPTY_MESSAGES: WsMessage[] = [];

export const useChatV2 = () => {
  const base = useChatStoreV2(
    useShallow((state) => {
      const activeConversation = state.activeConversationId
        ? state.conversations.find((conversation) => conversation.id === state.activeConversationId) ?? null
        : null;

      return {
        conversations: state.conversations,
        activeConversationId: state.activeConversationId,
        activeConversation,
        isGenerating: state.isGenerating,
        connectionStatus: state.connectionStatus,
        selectConversation: state.selectConversation,
        createConversation: state.createConversation,
        deleteConversation: state.deleteConversation,
        sendMessage: state.sendMessage,
        cancelCurrentTask: state.cancelCurrentTask,
        resetState: state.resetState,
        loadStartup: state.loadStartup,
      };
    }),
  );
  const messages = useChatStoreV2(
    useShallow((state) => (state.activeConversationId ? selectMessages(state, state.activeConversationId) : EMPTY_MESSAGES)),
  );

  return { ...base, messages };
};
