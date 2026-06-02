export { useChatStore } from './chatStore';
export type { ChatState } from './chatStore';
// [2026-05-31] Export the reducer-backed store beside the legacy store. Why: Step
// 2A keeps both stores available during migration. How: re-export only the new hook
// and its distinct V2 types. Purpose: avoid changing old imports while allowing new
// components and tests to consume chatStoreV2 explicitly.
export { useChatStoreV2 } from './chatStoreV2';
export type { ChatStoreV2State, ConversationMeta, ConnectionStatus } from './chatStoreV2';
export { useClientPrefsStore, shouldAutoApproveTool } from './clientPrefsStore';
export type { ClientPrefs, TitleGenerationMode } from './clientPrefsStore';
export { useSettingsStore } from './settingsStore';
export type { SettingsState } from './settingsStore';
// [2026-06-01] Export viewStore beside configuration stores.
// Why: shell routing is intentionally separate from settings data. How: expose the
// view hook and types from the store barrel. Purpose: tests and future views can
// consume the registry-driven view state without importing configuration state.
export { useViewStore } from './viewStore';
export type { ViewMode, ViewState } from './viewStore';
