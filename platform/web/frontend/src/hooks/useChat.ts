// [2026-05-16] Updated: exposes new store fields for stream preview, cancel, delete.
import { useShallow } from 'zustand/react/shallow';

import { useChatStore } from '../store/chatStore';

export const useChat = () =>
  useChatStore(
    useShallow((state) => ({
      conversations: state.conversations,
      activeConversationId: state.activeConversationId,
      activeConversation: state.activeConversation,
      typingConversationId: state.typingConversationId,
      isGenerating: state.isGenerating,
      streamPreview: state.streamPreview,
      sessionMap: state.sessionMap,
      selectConversation: state.selectConversation,
      createConversation: state.createConversation,
      deleteConversation: state.deleteConversation,
      sendActiveMessage: state.sendActiveMessage,
      cancelCurrentTask: state.cancelCurrentTask,
    })),
  );
