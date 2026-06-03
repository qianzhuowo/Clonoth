export {
  postInbound,
  // [2026-06-03] Export global realtime controls so stores subscribe once to all
  // Supervisor sessions instead of replacing per-session sockets.
  connectGlobalWS,
  disconnectGlobalWS,
  connectSessionWS,
  disconnectSessionWS,
  checkHealth,
  getAdminState,
  checkAdminAuth,
  getNodes,
  // [2026-06-01] Why: client settings can import API helpers through the public
  // barrel as other frontend code does. How: re-export the dynamic tool-name API
  // beside the existing admin config helpers. Purpose: future settings code does
  // not need to couple directly to supervisorClient.ts for this endpoint.
  getAllToolNames,
  getModelConfig,
  updateModelConfig,
  decideApproval,
  cancelActiveTasks,
  resetConversation,
} from './supervisorClient';
export type { AdminState, HealthState } from './supervisorClient';
