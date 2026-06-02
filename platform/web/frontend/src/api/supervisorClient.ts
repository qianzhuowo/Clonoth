// [2026-05-16] Real Supervisor API client — zero mock.
import type { NodeDef, SupervisorEvent } from '../types';

const API = '/v1';

// ── Helper ──

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const resp = await fetch(`${API}${path}`, init);
  if (!resp.ok) {
    let detail = '';
    try { const j = await resp.json(); detail = j.detail || ''; } catch { /* ignore */ }
    throw new Error(`${resp.status}${detail ? ` ${detail}` : ''}`);
  }
  return resp;
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

// ── Attachment upload ──

export interface UploadedAttachment {
  path: string;
  name: string;
  size: number;
  mime_type: string;
  type: 'image' | 'file';
}

export async function uploadAttachment(
  file: File,
  conversationKey: string,
): Promise<UploadedAttachment> {
  const form = new FormData();
  form.append('file', file);
  const resp = await fetch(
    `${API}/attachments/upload?conversation_key=${encodeURIComponent(conversationKey)}`,
    { method: 'POST', body: form },
  );
  if (!resp.ok) {
    let detail = '';
    try { const j = await resp.json(); detail = j.detail || ''; } catch { /* ignore */ }
    throw new Error(`Upload failed: ${resp.status}${detail ? ` ${detail}` : ''}`);
  }
  return resp.json();
}

// ── Inbound (send message) ──

export async function postInbound(params: {
  conversation_key: string;
  text: string;
  attachments?: any[];
  use_context?: boolean;
  entry_node_id?: string;
}): Promise<{ session_id: string; inbound_seq: number; accepted: boolean }> {
  const resp = await apiFetch('/inbound', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      channel: 'web',
      conversation_key: params.conversation_key,
      text: params.text,
      attachments: params.attachments ?? [],
      use_context: params.use_context ?? true,
      entry_node_id: params.entry_node_id,
    }),
  });
  return resp.json();
}

// ── Events polling ──

export async function pollEvents(sessionId: string, afterSeq = 0): Promise<SupervisorEvent[]> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}/events?after_seq=${afterSeq}`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── WebSocket events ──

// [2026-05-17] Phase 3 replaces active chat polling with one session WebSocket.
// These module-level handles make every new connection close the old one first,
// which prevents duplicated event delivery after the user sends another message.
let _ws: WebSocket | null = null;
let _wsReconnectTimer: ReturnType<typeof setTimeout> | null = null;
let _wsReconnectDelay = 1000;

export function connectSessionWS(
  sessionId: string,
  lastSeq: number,
  onEvent: (event: SupervisorEvent) => void,
  onOpen?: () => void,
  onDisconnect?: () => void,
): void {
  // [2026-05-17] The caller owns the latest sequence value. We still disconnect
  // here so reconnect attempts and conversation switches never leave stale sockets
  // pushing events into the current store state.
  disconnectSessionWS();

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/v1/sessions/${sessionId}/ws`;
  const ws = new WebSocket(wsUrl);
  _ws = ws;

  ws.onopen = () => {
    // [2026-05-17] The first client message is the catch-up cursor required by
    // Supervisor. Resetting the delay here makes later disconnect recovery start
    // from the smallest backoff again after a successful connection.
    _wsReconnectDelay = 1000;
    ws.send(JSON.stringify({ last_seq: lastSeq }));
    // [2026-06-01] Notify stores when the socket is actually open.
    // Why: waiting for the first non-ping event can leave the UI reporting a stale
    // disconnected or connecting state even though the transport is ready. How: call
    // this optional callback immediately after the catch-up cursor is sent. Purpose:
    // connection indicators reflect socket readiness, not event arrival timing.
    onOpen?.();
  };

  ws.onmessage = (msgEvent) => {
    try {
      const data = JSON.parse(msgEvent.data);
      if (data.type === 'ping') return;
      onEvent(data as SupervisorEvent);
    } catch {
      // [2026-05-17] Invalid frames are ignored because the event stream is
      // append-only and a malformed frame should not break future valid frames.
    }
  };

  ws.onclose = () => {
    _ws = null;
    onDisconnect?.();
    // [2026-05-17] Keep the backoff state in this client even though chatStore
    // decides whether to reconnect. The timer is intentionally a no-op marker so
    // disconnectSessionWS can cancel pending close state before a new socket opens.
    _wsReconnectTimer = setTimeout(() => {}, _wsReconnectDelay);
    _wsReconnectDelay = Math.min(_wsReconnectDelay * 2, 30000);
  };

  ws.onerror = () => {
    // [2026-05-17] Closing on errors funnels all cleanup through onclose, so the
    // store has one recovery path for network errors and normal disconnects.
    ws.close();
  };
}

export function disconnectSessionWS(): void {
  // [2026-05-17] This cleanup is called from stopPolling and before every new
  // connection. Clearing both the backoff timer and onclose handler prevents a
  // manual stop from scheduling another reconnect.
  if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
  if (_ws) {
    _ws.onclose = null;
    _ws.close();
    _ws = null;
  }
}

// ── Health ──

export interface HealthState {
  status: string;
  run_id?: string;
  workspace_root?: string;
  started_at?: string;
  uptime_seconds?: number;
}

export async function checkHealth(): Promise<HealthState> {
  const resp = await apiFetch('/health');
  return resp.json();
}

export interface AdminApproval {
  approval_id: string;
  session_id?: string;
  operation: string;
  details?: Record<string, unknown>;
  status?: string;
  fingerprint?: string;
  requested_at?: string;
  decided_at?: string | null;
  decision?: 'allow' | 'deny' | null;
  comment?: string | null;
  tool_call_id?: string | null;
  node_id?: string | null;
  task_id?: string | null;
}

export interface AdminState {
  sessions: number;
  approvals: Record<string, number>;
  tasks: Record<string, number>;
  pending_approvals: AdminApproval[];
  engine_runtime: Record<string, unknown>;
}

export interface AdminNode extends NodeDef {
  tool_access?: unknown;
  skills?: unknown;
}

export interface AdminTool {
  name: string;
  file?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  timeout_sec?: number;
  has_spec?: boolean;
}

export interface AdminSkill {
  name: string;
  description?: string;
  enabled?: boolean;
  strategy?: string;
  keywords?: string[];
  body_preview?: string;
  error?: string;
}

export interface McpClient {
  id: string;
  description?: string;
  enabled?: boolean;
  transport?: string;
  command?: string;
  args?: string[];
  env?: Record<string, unknown>;
  url?: string;
  headers?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface AdminCreatePayload {
  id: string;
  content: string;
}

async function readRawConfig(path: string, token: string): Promise<string> {
  // [2026-06-02] Shared raw-config reader for the expanded Settings tabs.
  // Why: nodes, tools, skills, MCP clients, schedules, policy, and runtime all expose
  // the same {content:string} shape. How: centralize bearer auth and response
  // unwrapping in one helper. Purpose: page code edits text without duplicating API
  // response handling or accidentally returning the wrapper object.
  const resp = await apiFetch(path, { headers: authHeaders(token) });
  const json = await resp.json();
  return typeof json.content === 'string' ? json.content : '';
}

async function writeRawConfig(path: string, token: string, content: string): Promise<any> {
  // [2026-06-02] Shared raw-config writer for the expanded Settings tabs.
  // Why: every raw editor saves through the same {content:string} backend model. How:
  // send JSON with the admin bearer header in one helper. Purpose: the UI can add new
  // raw-backed pages without reimplementing method, headers, and payload shape.
  const resp = await apiFetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ content }),
  });
  return resp.json();
}

const pathPart = (value: string): string => encodeURIComponent(value);

export async function getAdminState(token: string): Promise<AdminState> {
  // [2026-06-01] Fetch the protected Supervisor snapshot for the chat dashboard.
  // Why: the new right rail needs session, approval, task, and engine worker counts.
  // How: call /v1/admin/state with the existing bearer token helper. Purpose: keep
  // dashboard polling inside the API client instead of scattering endpoint strings.
  const resp = await apiFetch('/admin/state', { headers: authHeaders(token) });
  return resp.json();
}

// ── Admin auth ──

export async function checkAdminAuth(token: string): Promise<boolean> {
  try {
    const resp = await fetch(`${API}/admin/auth/check`, { headers: authHeaders(token) });
    return resp.ok;
  } catch {
    return false;
  }
}

// ── Nodes ──

export async function getNodes(token: string): Promise<AdminNode[]> {
  const resp = await apiFetch('/admin/config/nodes', { headers: authHeaders(token) });
  return resp.json();
}

export function getRuntimeRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/runtime/raw', token);
}

export function updateRuntimeRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/runtime/raw', token, yaml);
}

export function getPolicyRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/policy/raw', token);
}

export function updatePolicyRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/policy/raw', token, yaml);
}

export function getSchedulesRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/schedules/raw', token);
}

export function updateSchedulesRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/schedules/raw', token, yaml);
}

export function getNodeRaw(token: string, nodeId: string): Promise<string> {
  return readRawConfig(`/admin/config/nodes/${pathPart(nodeId)}/raw`, token);
}

export function updateNodeRaw(token: string, nodeId: string, yaml: string): Promise<any> {
  return writeRawConfig(`/admin/config/nodes/${pathPart(nodeId)}/raw`, token, yaml);
}

export async function createNode(token: string, data: AdminCreatePayload): Promise<any> {
  // [2026-06-02] Create raw-backed node files through the existing admin endpoint.
  // Why: the backend currently accepts an id plus complete YAML content instead of a
  // higher-level template object. How: keep the wrapper close to that contract while
  // pages may build the content from a selected template. Purpose: node creation stays
  // compatible with Supervisor without adding another server schema.
  const resp = await apiFetch('/admin/config/nodes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteNode(token: string, nodeId: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/nodes/${pathPart(nodeId)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getTools(token: string): Promise<AdminTool[]> {
  const resp = await apiFetch('/admin/config/tools', { headers: authHeaders(token) });
  return resp.json();
}

export function getToolRaw(token: string, name: string): Promise<string> {
  return readRawConfig(`/admin/config/tools/${pathPart(name)}/raw`, token);
}

export function updateToolRaw(token: string, name: string, script: string): Promise<any> {
  return writeRawConfig(`/admin/config/tools/${pathPart(name)}/raw`, token, script);
}

export async function createTool(token: string, data: AdminCreatePayload): Promise<any> {
  const resp = await apiFetch('/admin/config/tools', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteTool(token: string, name: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/tools/${pathPart(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getSkills(token: string): Promise<AdminSkill[]> {
  const resp = await apiFetch('/admin/config/skills', { headers: authHeaders(token) });
  return resp.json();
}

export function getSkillRaw(token: string, name: string): Promise<string> {
  return readRawConfig(`/admin/config/skills/${pathPart(name)}/raw`, token);
}

export function updateSkillRaw(token: string, name: string, content: string): Promise<any> {
  return writeRawConfig(`/admin/config/skills/${pathPart(name)}/raw`, token, content);
}

export async function createSkill(token: string, data: AdminCreatePayload): Promise<any> {
  const resp = await apiFetch('/admin/config/skills', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(data),
  });
  return resp.json();
}

export async function deleteSkill(token: string, name: string): Promise<any> {
  const resp = await apiFetch(`/admin/config/skills/${pathPart(name)}`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getMcpClients(token: string): Promise<McpClient[]> {
  const resp = await apiFetch('/admin/config/mcp-clients', { headers: authHeaders(token) });
  return resp.json();
}

export function getMcpClientsRaw(token: string): Promise<string> {
  return readRawConfig('/admin/config/mcp-clients/raw', token);
}

export function updateMcpClientsRaw(token: string, yaml: string): Promise<any> {
  return writeRawConfig('/admin/config/mcp-clients/raw', token, yaml);
}

export async function restartEngine(token: string): Promise<any> {
  // [2026-06-02] Expose restart as an explicit engine-only wrapper for Settings.
  // Why: the UI must not guess the RestartIn payload each time. How: send the
  // backend-required target and a Chinese reason string with admin auth. Purpose: the
  // dangerous action remains behind a confirm dialog while the API contract is fixed.
  const resp = await apiFetch('/admin/restart', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify({ target: 'engine', reason: '用户从设置页面请求重启引擎' }),
  });
  return resp.json();
}

export async function reloadConfig(token: string): Promise<any> {
  const resp = await apiFetch('/config/reload', {
    method: 'POST',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function reloadTools(token: string): Promise<any> {
  const resp = await apiFetch('/tools/reload', {
    method: 'POST',
    headers: authHeaders(token),
  });
  return resp.json();
}

export async function getAllToolNames(token: string): Promise<string[]> {
  // [2026-06-01] Why: approval preferences must list the backend's complete tool
  // set instead of a stale frontend constant. How: call the protected Supervisor
  // all-tool-names endpoint with the existing bearer header helper. Purpose: the
  // client settings page can show recommended tools plus every other tool returned
  // by the running server, while failed auth still falls back in the UI.
  const resp = await apiFetch('/admin/config/all-tool-names', { headers: authHeaders(token) });
  return resp.json();
}

// ── Model config ──

export async function getModelConfig(token: string): Promise<{
  model: string;
  base_url: string;
  api_key_present: boolean;
  api_key?: string;
}> {
  const resp = await apiFetch('/config/openai/secret', { headers: authHeaders(token) });
  return resp.json();
}

export async function updateModelConfig(
  token: string,
  params: { model?: string; base_url?: string; api_key?: string },
): Promise<any> {
  const resp = await apiFetch('/config/openai', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(params),
  });
  return resp.json();
}

export type SessionProviderOverride = Record<string, unknown>;

export async function getSessionProviderOverride(sessionId: string, token: string): Promise<SessionProviderOverride> {
  // [2026-06-01] Fetch session-scoped provider overrides.
  // Why: the right panel must show the effective session model/base_url instead of
  // only the global OpenAI defaults. How: call Supervisor's admin-protected
  // provider_override endpoint with the same bearer token used by config APIs.
  // Purpose: model edits can affect only the selected session.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, { headers: authHeaders(token) });
  return resp.json();
}

export async function updateSessionProviderOverride(
  sessionId: string,
  token: string,
  params: SessionProviderOverride,
): Promise<SessionProviderOverride> {
  // [2026-06-01] Save session-scoped provider overrides.
  // Why: global model updates are too broad for the requested session panel. How:
  // PUT the complete override object, preserving fields the compact editor does not
  // touch. Purpose: allow per-session model, provider, api_key, and base_url edits.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders(token) },
    body: JSON.stringify(params),
  });
  return resp.json();
}

export async function clearSessionProviderOverride(sessionId: string, token: string): Promise<SessionProviderOverride> {
  // [2026-06-01] Clear session-scoped provider overrides.
  // Why: the session panel needs a direct way back to node/global defaults. How:
  // call DELETE on the same provider_override resource. Purpose: avoid saving empty
  // model fields that would be hard to distinguish from inherited defaults.
  const resp = await apiFetch(`/sessions/${sessionId}/provider_override`, {
    method: 'DELETE',
    headers: authHeaders(token),
  });
  return resp.json();
}

// ── Approvals ──

export async function decideApproval(
  approvalId: string,
  decision: 'allow' | 'deny',
  comment = '',
): Promise<any> {
  const resp = await apiFetch(`/approvals/${approvalId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, comment: comment || `${decision} via web` }),
  });
  return resp.json();
}

// ── Cancel ──

export async function cancelActiveTasks(sessionId: string): Promise<any> {
  const resp = await apiFetch(`/sessions/${sessionId}/cancel_active_tasks`, { method: 'POST' });
  return resp.json();
}

// ── Reset conversation ──

export async function resetConversation(conversationKey: string): Promise<any> {
  const resp = await apiFetch('/conversations/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_key: conversationKey }),
  });
  return resp.json();
}

// ── Active node ──

export async function getActiveNode(sessionId: string): Promise<{
  node_id: string;
  is_override: boolean;
  default_node_id: string;
}> {
  const resp = await apiFetch(`/sessions/${sessionId}/active_node`);
  return resp.json();
}

export async function switchNode(sessionId: string, targetNodeId: string): Promise<any> {
  const resp = await apiFetch(`/sessions/${sessionId}/switch_node`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target_node_id: targetNodeId }),
  });
  return resp.json();
}

// ── App config (public, no auth needed) ──

export interface AppConfigPublic {
  version?: number;
  provider: string;
  openai: { model: string; base_url: string; api_key_present?: boolean; api_key?: string };
  entry_node_id?: string;
  default_entry_node_id?: string;
  shell?: { entry_node_id?: string };
}

export async function getConfig(): Promise<AppConfigPublic> {
  // [2026-06-02] Expose the public config endpoint under its route name as well as
  // getAppConfig. Why: the Client settings page needs to read the currently
  // configured entry_node_id when the backend includes it, while older callers still
  // use getAppConfig for model display. How: return the same /v1/config response with
  // optional entry-node fields in the TypeScript shape. Purpose: selection state can
  // prefer real Supervisor configuration without breaking existing config consumers.
  const resp = await apiFetch('/config');
  return resp.json();
}

export function getAppConfig(): Promise<AppConfigPublic> {
  // [2026-06-02] Keep the historical helper as an alias to getConfig.
  // Why: Header and session panels already call getAppConfig. How: delegate to the
  // route-named wrapper instead of duplicating fetch logic. Purpose: both old and new
  // settings code share one public config contract.
  return getConfig();
}

// ── List sessions ──

export interface SessionListItem {
  session_id: string;
  conversation_key: string;
  channel: string;
  created_at: string;
  updated_at: string;
}

export async function listSessions(channel = 'web', limit = 50): Promise<SessionListItem[]> {
  try {
    const resp = await fetch(`${API}/sessions?channel=${channel}&limit=${limit}`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── Delete session ──

export async function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}`, { method: 'DELETE' });
    if (!resp.ok) return { ok: false };
    return resp.json();
  } catch {
    return { ok: false };
  }
}

// ── Session messages (legacy, flat text) ──

export async function getSessionMessages(sessionId: string, limit = 200): Promise<{ role: string; content: string }[]> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}/messages?limit=${limit}`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── Structured history (ConversationStore) ──

export interface StructuredMessage {
  id: string;
  role: string;
  content: string;
  message_type?: string;
  created_at?: string;
  source_node_id?: string;
  thinking?: string;
  // Clonoth format: {id, name, arguments(object)}
  tool_calls?: Array<{ id?: string; name: string; arguments?: Record<string, unknown> }>;
  tool_call_id?: string;
  tool_name?: string;
  name?: string;
  // [thinking-time 2026-06-01] Precise reasoning timing from backend meta.
  reasoning_started_at?: string;
  reasoning_ended_at?: string;
}

export async function getSessionHistory(sessionId: string, limit = 200): Promise<StructuredMessage[]> {
  try {
    const resp = await fetch(`${API}/sessions/${sessionId}/history?limit=${limit}`);
    if (!resp.ok) return [];
    return resp.json();
  } catch {
    return [];
  }
}

// ── Legacy compat exports ──

export const sendInbound = postInbound;
export const getEvents = pollEvents;
