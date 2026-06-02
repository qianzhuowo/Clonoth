// This hook barrel is added to provide one import path for the current and future web hooks.
// It has no runtime side effects; it only re-exports the chat and session adapters.
// The purpose is to keep component imports simple as more Supervisor hooks are added.
// [2026-05-31] Export useChatV2 beside useChat. Why: Step 2A must keep the legacy
// hook untouched while making the reducer-backed adapter available to new callers.
// How: add a separate named export. Purpose: later migration can opt in file by file.
export { useChat } from './useChat';
export { useChatV2 } from './useChatV2';
export { useSession } from './useSession';
