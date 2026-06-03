// [2026-06-02] Shared lightweight structured-config helpers for Settings.
// Why: the requested Settings pages need parsed forms, but the frontend intentionally
// has no YAML dependency. How: support the simple object/list shapes used by
// runtime.providers, schedules.yaml, MCP clients, and skill frontmatter, while keeping
// raw YAML fallback editors available. Purpose: operators can edit common fields in
// forms without losing a raw escape hatch for unsupported syntax.

export type ScalarValue = string | number | boolean | null;
export type SimpleYamlValue = ScalarValue | SimpleYamlObject | SimpleYamlValue[];
export interface SimpleYamlObject { [key: string]: SimpleYamlValue; }

export interface ParsedProvidersResult {
  providers: Record<string, SimpleYamlObject>;
  rawProviders: string;
}

export interface ScheduleFormState {
  id: string;
  cron: string;
  type: 'message' | 'script';
  text: string;
  command: string;
  enabled: boolean;
  once: boolean;
  conversation_key: string;
  entry_node_id: string;
  workflow_id: string;
  timeout: string;
  silent: boolean;
}

export interface McpClientFormState {
  id: string;
  description: string;
  enabled: boolean;
  transport: 'stdio' | 'sse' | 'streamable_http';
  command: string;
  argsText: string;
  envText: string;
  url: string;
  headersText: string;
}

export interface SkillFormState {
  name: string;
  description: string;
  enabled: boolean;
  strategy: 'normal' | 'constant';
  keywordsText: string;
  order: string;
  priority: string;
  scan_depth: string;
  body: string;
}

export type NodeConfigType = 'ai' | 'tool' | 'router';
export type ToolAccessMode = 'all' | 'allow' | 'deny' | 'none';

export interface NodeConfigFormState {
  id: string;
  name: string;
  description: string;
  type: NodeConfigType;
  model: string;
  provider: string;
  memory_book: string;
  persistent: boolean;
  prompt: string;
  delegate_targetsText: string;
  tool_access_mode: ToolAccessMode;
  tool_access_allowText: string;
  tool_access_denyText: string;
}

export interface RuntimeConfigFormState {
  entry_node_id: string;
  tool_mode: ToolAccessMode;
  max_concurrent_tasks: string;
}

function splitLines(text: string): string[] {
  return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');
}

function indentOf(line: string): number {
  return line.match(/^ */)?.[0].length || 0;
}

function stripCommentOutsideQuotes(value: string): string {
  let quote = '';
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    const prev = value[index - 1];
    if ((char === '"' || char === "'") && prev !== '\\') {
      quote = quote === char ? '' : quote || char;
    }
    if (char === '#' && !quote && (index === 0 || /\s/.test(value[index - 1]))) {
      return value.slice(0, index).trimEnd();
    }
  }
  return value.trimEnd();
}

function unquote(value: string): string {
  const trimmed = value.trim();
  if ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed.slice(1, -1).replace(/\\"/g, '"').replace(/\\'/g, "'");
  }
  return trimmed;
}

function parseScalar(value: string): ScalarValue | string[] {
  const clean = stripCommentOutsideQuotes(value).trim();
  if (clean === '') return '';
  if (clean === 'null' || clean === '~') return null;
  if (clean === 'true') return true;
  if (clean === 'false') return false;
  if (/^-?\d+(\.\d+)?$/.test(clean)) return Number(clean);
  if (clean.startsWith('[') && clean.endsWith(']')) {
    const inner = clean.slice(1, -1).trim();
    if (!inner) return [];
    return splitTopLevelComma(inner).map((item) => String(parseScalar(item.trim()) ?? ''));
  }
  return unquote(clean);
}

function splitTopLevelComma(value: string): string[] {
  const result: string[] = [];
  let current = '';
  let quote = '';
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    const prev = value[index - 1];
    if ((char === '"' || char === "'") && prev !== '\\') quote = quote === char ? '' : quote || char;
    if (char === ',' && !quote) {
      result.push(current);
      current = '';
      continue;
    }
    current += char;
  }
  result.push(current);
  return result;
}

function formatScalar(value: SimpleYamlValue): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return String(value);
  if (value === null || value === undefined) return "''";
  const text = String(value);
  if (text === '') return "''";
  if (/^[A-Za-z0-9_./:@?=&+*%\-]+$/.test(text) && !['true', 'false', 'null'].includes(text)) return text;
  return JSON.stringify(text);
}

function writeValue(key: string, value: SimpleYamlValue, indent: number): string[] {
  const prefix = ' '.repeat(indent);
  if (Array.isArray(value)) {
    if (value.length === 0) return [`${prefix}${key}: []`];
    return [`${prefix}${key}:`, ...value.map((item) => `${prefix}  - ${formatScalar(item)}`)];
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as SimpleYamlObject);
    if (entries.length === 0) return [`${prefix}${key}: {}`];
    return [`${prefix}${key}:`, ...entries.flatMap(([childKey, childValue]) => writeValue(childKey, childValue, indent + 2))];
  }
  return [`${prefix}${key}: ${formatScalar(value)}`];
}

function parseSimpleMap(lines: string[], start: number, parentIndent: number): { value: SimpleYamlObject; next: number } {
  const result: SimpleYamlObject = {};
  let index = start;
  while (index < lines.length) {
    const rawLine = lines[index];
    if (!rawLine.trim() || rawLine.trimStart().startsWith('#')) { index += 1; continue; }
    const indent = indentOf(rawLine);
    if (indent <= parentIndent) break;
    const trimmed = rawLine.trim();
    const match = trimmed.match(/^([^:]+):(.*)$/);
    if (!match) { index += 1; continue; }
    const key = unquote(match[1].trim());
    const rest = match[2].trim();
    if (rest === '') {
      const nextMeaningful = findNextMeaningfulLine(lines, index + 1);
      if (nextMeaningful >= 0 && indentOf(lines[nextMeaningful]) >= indent && lines[nextMeaningful].trimStart().startsWith('- ')) {
        // [2026-06-02] Accept YAML lists that start at the same indentation as the
        // key line. Why: existing schedules.yaml and mcp_clients.yaml use
        // `key:\n- item` and `args:\n    - item` shapes. How: treat same-indent dash
        // lines as child list content. Purpose: the structured forms parse deployed
        // config files instead of only pretty-printed variants.
        const parsed = parseSimpleList(lines, index + 1, indent);
        result[key] = parsed.value;
        index = parsed.next;
      } else {
        const parsed = parseSimpleMap(lines, index + 1, indent);
        result[key] = parsed.value;
        index = parsed.next;
      }
      continue;
    }
    result[key] = parseScalar(rest) as SimpleYamlValue;
    index += 1;
  }
  return { value: result, next: index };
}

function parseSimpleList(lines: string[], start: number, parentIndent: number): { value: SimpleYamlValue[]; next: number } {
  const result: SimpleYamlValue[] = [];
  let index = start;
  while (index < lines.length) {
    const rawLine = lines[index];
    if (!rawLine.trim() || rawLine.trimStart().startsWith('#')) { index += 1; continue; }
    const indent = indentOf(rawLine);
    // [2026-06-02] Allow same-indent dash items below a key. Why: deployed YAML uses
    // `schedules:\n- id` and nested `args:\n    - value` forms. How: stop only when
    // indentation falls below the parent key, then require the dash marker. Purpose:
    // common schedules and MCP client lists parse without needing a YAML dependency.
    if (indent < parentIndent) break;
    const trimmed = rawLine.trim();
    if (!trimmed.startsWith('- ')) break;
    const afterDash = trimmed.slice(2).trim();
    if (!afterDash) {
      const parsed = parseSimpleMap(lines, index + 1, indent);
      result.push(parsed.value);
      index = parsed.next;
      continue;
    }
    const inlinePair = afterDash.match(/^([^:]+):(.*)$/);
    if (inlinePair) {
      const item: SimpleYamlObject = {};
      const key = unquote(inlinePair[1].trim());
      const rest = inlinePair[2].trim();
      item[key] = rest ? parseScalar(rest) as SimpleYamlValue : '';
      const parsed = parseSimpleMap(lines, index + 1, indent);
      Object.assign(item, parsed.value);
      result.push(item);
      index = parsed.next;
      continue;
    }
    result.push(parseScalar(afterDash) as SimpleYamlValue);
    index += 1;
  }
  return { value: result, next: index };
}

function findNextMeaningfulLine(lines: string[], start: number): number {
  for (let index = start; index < lines.length; index += 1) {
    const trimmed = lines[index].trim();
    if (trimmed && !trimmed.startsWith('#')) return index;
  }
  return -1;
}

function normalizeToolAccessMode(value: string, fallback: ToolAccessMode = 'all'): ToolAccessMode {
  // [2026-06-02] Keep form selects inside the supported tool-mode set.
  // Why: node and runtime YAML may contain comments, blanks, or older values. How:
  // accept only all, allow, deny, and none, then fall back to the caller default.
  // Purpose: structured forms do not write unsupported select values back to YAML.
  return value === 'allow' || value === 'deny' || value === 'none' || value === 'all' ? value : fallback;
}

function normalizeNodeConfigType(value: string): NodeConfigType {
  // [2026-06-02] Keep node type editing constrained to supported Settings values.
  // Why: the Agents right panel exposes a select instead of raw YAML as the primary
  // editor. How: normalize parsed YAML into ai, tool, or router. Purpose: malformed or
  // missing raw values cannot put the form in an invalid select state.
  return value === 'tool' || value === 'router' || value === 'ai' ? value : 'ai';
}

function commaTextToItems(value: string): string[] {
  // [2026-06-02] Convert comma fields into YAML list items.
  // Why: the requested structured forms use comma-separated text controls for common
  // list fields. How: split on English commas and trim empty entries. Purpose: users
  // can edit delegate and tool lists without manually writing YAML list syntax.
  return value.split(',').map((item) => item.trim()).filter(Boolean);
}

function scalarLineValue(raw: string, key: string, fallback = ''): string {
  // [2026-06-02] Read a top-level scalar with the simple regex approach requested.
  // Why: the frontend intentionally avoids js-yaml. How: match `key: value`, strip
  // simple quotes and comments, and leave unsupported YAML to the raw fallback editor.
  // Purpose: common node and runtime fields populate independent form controls.
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = raw.match(new RegExp(`^${escaped}:\\s*["']?([^"'\\n#]+)`, 'm'));
  return match?.[1]?.trim() || fallback;
}

function booleanLineValue(raw: string, key: string, fallback = false): boolean {
  // [2026-06-02] Parse a simple top-level YAML boolean for checkbox controls.
  // Why: persistent is edited as a checkbox, not raw text. How: read only explicit
  // true or false values and use the fallback when the key is missing. Purpose: the
  // form state stays predictable for existing node files with optional persistence.
  const value = scalarLineValue(raw, key, '');
  if (value === 'true') return true;
  if (value === 'false') return false;
  return fallback;
}

function listLineValue(raw: string, key: string): string[] {
  // [2026-06-02] Parse the simple YAML list shapes used by node delegates and tools.
  // Why: the Agents form uses comma-separated inputs while node YAML stores lists.
  // How: support both `key: [a, b]` and the requested block list format. Purpose:
  // common deployed configs parse without adding a YAML parser dependency.
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const inline = raw.match(new RegExp(`^${escaped}:\\s*\\[([^\\]]*)\\]`, 'm'));
  if (inline) return splitTopLevelComma(inline[1]).map((item) => unquote(item.trim())).filter(Boolean);
  const block = raw.match(new RegExp(`^${escaped}:\\s*\\n((?:\\s*-\\s*[^\\n#]+\\n?)*)`, 'm'));
  if (!block) return [];
  return splitLines(block[1]).map((line) => line.trim().replace(/^-\s*/, '')).map((item) => unquote(item.trim())).filter(Boolean);
}

function topLevelBlock(raw: string, key: string): string {
  // [2026-06-02] Extract one top-level YAML section for nested field parsing.
  // Why: tool_access contains mode and optional allow/deny lists below a parent key.
  // How: find the top-level key and collect following indented lines until the next
  // top-level section. Purpose: nested form fields can be parsed with lightweight
  // string logic while unsupported sections remain available in raw YAML.
  const lines = splitLines(raw);
  const start = lines.findIndex((line) => new RegExp(`^${key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}:`).test(line));
  if (start < 0) return '';
  let end = start + 1;
  while (end < lines.length) {
    const line = lines[end];
    if (line.trim() && indentOf(line) === 0) break;
    end += 1;
  }
  return lines.slice(start, end).join('\n');
}

function deindentChildBlock(block: string): string {
  // [2026-06-02] Normalize nested YAML before reusing top-level regex helpers.
  // Why: tool_access children are indented, but the lightweight scalar/list readers
  // intentionally match keys at the beginning of a line. How: remove the parent line
  // and one two-space indentation level. Purpose: nested mode, allow, and deny fields
  // parse into the same structured controls as top-level fields.
  return splitLines(block).slice(1).map((line) => line.replace(/^ {2}/, '')).join('\n');
}

function replaceTopLevelSection(raw: string, key: string, replacement: string): string {
  // [2026-06-02] Replace one top-level YAML key without reserializing the file.
  // Why: structured forms should update their fields while preserving unrelated
  // comments and advanced settings. How: splice the located scalar or indented block,
  // or append the section when it is missing. Purpose: saves have the smallest useful
  // blast radius despite the intentionally simple parser.
  const lines = splitLines(raw);
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const start = lines.findIndex((line) => new RegExp(`^${escaped}:`).test(line));
  const replacementLines = replacement.replace(/\n$/, '').split('\n');
  if (start < 0) {
    const prefix = raw && !raw.endsWith('\n') ? `${raw}\n` : raw;
    return `${prefix}${replacementLines.join('\n')}\n`;
  }
  let end = start + 1;
  while (end < lines.length) {
    const line = lines[end];
    if (line.trim() && indentOf(line) === 0) break;
    end += 1;
  }
  const joined = [...lines.slice(0, start), ...replacementLines, ...lines.slice(end)].join('\n');
  return raw.endsWith('\n') && !joined.endsWith('\n') ? `${joined}\n` : joined;
}

function removeTopLevelSection(raw: string, key: string): string {
  // [2026-06-02] Remove optional scalar sections when their form fields are blank.
  // Why: provider and memory_book are optional node fields. How: splice the existing
  // top-level section only when the key is present. Purpose: clearing a form input
  // removes the optional YAML key instead of writing an empty setting.
  const lines = splitLines(raw);
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const start = lines.findIndex((line) => new RegExp(`^${escaped}:`).test(line));
  if (start < 0) return raw;
  let end = start + 1;
  while (end < lines.length) {
    const line = lines[end];
    if (line.trim() && indentOf(line) === 0) break;
    end += 1;
  }
  const joined = [...lines.slice(0, start), ...lines.slice(end)].join('\n');
  return raw.endsWith('\n') && !joined.endsWith('\n') ? `${joined}\n` : joined;
}

function setOptionalScalar(raw: string, key: string, value: string): string {
  // [2026-06-02] Save optional node scalar fields only when they have values.
  // Why: empty provider or memory_book should not become misleading YAML entries.
  // How: remove the section when blank, otherwise replace or append a formatted
  // scalar line. Purpose: the structured editor keeps optional fields genuinely
  // optional while still supporting creation through the form.
  const trimmed = value.trim();
  if (!trimmed) return removeTopLevelSection(raw, key);
  return replaceTopLevelSection(raw, key, `${key}: ${formatScalar(trimmed)}`);
}

function serializeYamlListBlock(key: string, values: string[], indent = 0): string {
  // [2026-06-02] Write simple YAML lists from structured comma fields.
  // Why: delegate_targets and tool allow/deny values are edited as text inputs. How:
  // emit block-list YAML when values exist and `[]` for an empty list. Purpose: saved
  // YAML stays readable and matches the requested list representation.
  const prefix = ' '.repeat(indent);
  if (values.length === 0) return `${prefix}${key}: []`;
  return [`${prefix}${key}:`, ...values.map((value) => `${prefix}  - ${formatScalar(value)}`)].join('\n');
}

export function parseNodeConfig(raw: string, fallbackId = ''): NodeConfigFormState {
  // [2026-06-02] Parse selected node YAML into the Agents structured form.
  // Why: the right panel must edit fields directly instead of making raw YAML the
  // primary editor. How: use regex scalars plus simple block-list parsing for the
  // requested fields. Purpose: common node properties can be edited safely while the
  // advanced YAML details section remains available for unsupported syntax.
  const toolBlock = deindentChildBlock(topLevelBlock(raw, 'tool_access'));
  const toolMode = normalizeToolAccessMode(scalarLineValue(toolBlock, 'mode', 'all'));
  return {
    id: scalarLineValue(raw, 'id', fallbackId),
    name: scalarLineValue(raw, 'name', ''),
    description: scalarLineValue(raw, 'description', ''),
    type: normalizeNodeConfigType(scalarLineValue(raw, 'type', 'ai')),
    model: scalarLineValue(raw, 'model', ''),
    provider: scalarLineValue(raw, 'provider', ''),
    memory_book: scalarLineValue(raw, 'memory_book', ''),
    persistent: booleanLineValue(raw, 'persistent', false),
    delegate_targetsText: listLineValue(raw, 'delegate_targets').join(', '),
    tool_access_mode: toolMode,
    tool_access_allowText: listLineValue(toolBlock, 'allow').join(', '),
    tool_access_denyText: listLineValue(toolBlock, 'deny').join(', '),
    prompt: deindentChildBlock(topLevelBlock(raw, 'prompt')).trim(),
  };
}

export function serializeNodeConfig(raw: string, form: NodeConfigFormState): string {
  // [2026-06-02] Serialize the Agents structured form back into node YAML.
  // Why: saving must preserve unrelated advanced YAML while updating the edited
  // fields. How: replace top-level scalar/list sections and rebuild the tool_access
  // block from mode plus the relevant allow or deny list. Purpose: the UI can save
  // form controls through updateNodeRaw without making raw YAML the main editor.
  let next = raw || '';
  next = replaceTopLevelSection(next, 'id', `id: ${formatScalar(form.id.trim())}`);
  next = replaceTopLevelSection(next, 'name', `name: ${formatScalar(form.name.trim())}`);
  next = replaceTopLevelSection(next, 'description', `description: ${formatScalar(form.description.trim())}`);
  next = replaceTopLevelSection(next, 'type', `type: ${formatScalar(form.type)}`);
  next = replaceTopLevelSection(next, 'model', `model: ${formatScalar(form.model.trim())}`);
  next = setOptionalScalar(next, 'provider', form.provider);
  next = setOptionalScalar(next, 'memory_book', form.memory_book);
  next = replaceTopLevelSection(next, 'persistent', `persistent: ${form.persistent ? 'true' : 'false'}`);
  next = replaceTopLevelSection(next, 'delegate_targets', serializeYamlListBlock('delegate_targets', commaTextToItems(form.delegate_targetsText)));
  const mode = normalizeToolAccessMode(form.tool_access_mode);
  const toolLines = ['tool_access:', `  mode: ${mode}`];
  if (mode === 'allow') toolLines.push(serializeYamlListBlock('allow', commaTextToItems(form.tool_access_allowText), 2));
  if (mode === 'deny') toolLines.push(serializeYamlListBlock('deny', commaTextToItems(form.tool_access_denyText), 2));
  next = replaceTopLevelSection(next, 'tool_access', toolLines.join('\n'));
  return next.endsWith('\n') ? next : `${next}\n`;
}

export function parseRuntimeConfig(raw: string): RuntimeConfigFormState {
  // [2026-06-02] Parse runtime.yaml common fields for the Advanced structured form.
  // Why: runtime editing should expose the requested fields directly. How: use the
  // specified scalar regexes for entry_node_id, tool_mode, and max_concurrent_tasks.
  // Purpose: operators can change common runtime settings without editing raw YAML.
  return {
    entry_node_id: scalarLineValue(raw, 'entry_node_id', ''),
    tool_mode: normalizeToolAccessMode(scalarLineValue(raw, 'tool_mode', 'all')),
    max_concurrent_tasks: scalarLineValue(raw, 'max_concurrent_tasks', ''),
  };
}

export function serializeRuntimeConfig(raw: string, form: RuntimeConfigFormState): string {
  // [2026-06-02] Serialize the runtime structured fields back into runtime.yaml.
  // Why: the Advanced page must save through updateRuntimeRaw while keeping unrelated
  // runtime sections intact. How: replace or append the three common scalar keys only.
  // Purpose: structured runtime editing has a raw YAML fallback without becoming a
  // whole-file YAML editor.
  let next = raw || '';
  next = replaceTopLevelSection(next, 'entry_node_id', `entry_node_id: ${formatScalar(form.entry_node_id.trim())}`);
  next = replaceTopLevelSection(next, 'tool_mode', `tool_mode: ${formatScalar(normalizeToolAccessMode(form.tool_mode))}`);
  next = replaceTopLevelSection(next, 'max_concurrent_tasks', `max_concurrent_tasks: ${form.max_concurrent_tasks.trim() || '0'}`);
  return next.endsWith('\n') ? next : `${next}\n`;
}

export function parseProvidersFromRuntime(raw: string): ParsedProvidersResult {
  // [2026-06-02] Extract runtime.providers for the Model tab provider editor. Why:
  // runtime.yaml has many unrelated sections and comments. How: locate the top-level
  // providers block, parse its simple nested map, and also preserve the raw slice.
  // Purpose: provider configs can be edited as structured JSON and written back.
  const lines = splitLines(raw);
  const start = lines.findIndex((line) => /^providers:\s*$/.test(line.trimEnd()));
  if (start < 0) return { providers: {}, rawProviders: 'providers: {}' };
  let end = lines.length;
  for (let index = start + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim()) continue;
    if (indentOf(line) === 0 && !line.trimStart().startsWith('#')) { end = index; break; }
  }
  const parsed = parseSimpleMap(lines, start + 1, 0).value;
  const providers: Record<string, SimpleYamlObject> = {};
  Object.entries(parsed).forEach(([key, value]) => {
    providers[key] = value && typeof value === 'object' && !Array.isArray(value) ? value as SimpleYamlObject : { value: value as ScalarValue };
  });
  return { providers, rawProviders: lines.slice(start, end).join('\n') };
}

export function serializeProvidersBlock(providers: Record<string, SimpleYamlObject>): string {
  const entries = Object.entries(providers).filter(([name]) => name.trim());
  if (entries.length === 0) return 'providers: {}';
  return ['providers:', ...entries.flatMap(([name, value]) => writeValue(name, value || {}, 2))].join('\n');
}

export function replaceProvidersInRuntime(raw: string, providers: Record<string, SimpleYamlObject>): string {
  // [2026-06-02] Replace only the providers block in runtime.yaml. Why: the Model tab
  // must not rewrite unrelated runtime comments or settings. How: splice the located
  // top-level section with the serialized provider block. Purpose: saving provider
  // forms has the smallest practical blast radius.
  const lines = splitLines(raw);
  const block = serializeProvidersBlock(providers).split('\n');
  const start = lines.findIndex((line) => /^providers:\s*(?:#.*)?$/.test(line.trimEnd()) || /^providers:\s*\{\}\s*$/.test(line.trimEnd()));
  if (start < 0) {
    const needsNewline = raw.endsWith('\n') || raw.length === 0 ? '' : '\n';
    return `${raw}${needsNewline}\n${block.join('\n')}\n`;
  }
  let end = lines.length;
  for (let index = start + 1; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim()) continue;
    if (indentOf(line) === 0 && !line.trimStart().startsWith('#')) { end = index; break; }
  }
  return [...lines.slice(0, start), ...block, ...lines.slice(end)].join('\n');
}

export function parseSchedules(raw: string): ScheduleFormState[] {
  // [2026-06-02] Parse schedules.yaml into editable task forms. Why: the Automation
  // tab now shows field controls instead of only raw YAML. How: read the top-level
  // schedules list and normalize optional fields. Purpose: common task edits are safe
  // and discoverable while the raw fallback still covers unusual fields.
  const lines = splitLines(raw);
  const start = lines.findIndex((line) => /^schedules:\s*/.test(line.trimEnd()));
  if (start < 0) return [];
  const list = parseSimpleList(lines, start + 1, 0).value;
  return list.filter((item): item is SimpleYamlObject => Boolean(item && typeof item === 'object' && !Array.isArray(item))).map((item) => ({
    id: String(item.id || ''),
    cron: String(item.cron || ''),
    type: item.type === 'script' ? 'script' : 'message',
    text: String(item.text || ''),
    command: String(item.command || ''),
    enabled: item.enabled !== false,
    once: item.once === true,
    conversation_key: String(item.conversation_key || ''),
    entry_node_id: String(item.entry_node_id || ''),
    workflow_id: String(item.workflow_id || ''),
    timeout: item.timeout === undefined ? '' : String(item.timeout),
    silent: item.silent === true,
  }));
}

export function serializeSchedules(schedules: ScheduleFormState[]): string {
  const normalized = schedules.map((schedule) => {
    const item: SimpleYamlObject = {
      id: schedule.id,
      cron: schedule.cron,
      type: schedule.type,
      text: schedule.text,
      enabled: schedule.enabled,
      once: schedule.once,
    };
    if (schedule.conversation_key) item.conversation_key = schedule.conversation_key;
    if (schedule.entry_node_id) item.entry_node_id = schedule.entry_node_id;
    if (schedule.workflow_id) item.workflow_id = schedule.workflow_id;
    if (schedule.type === 'script') {
      item.command = schedule.command;
      if (schedule.timeout) item.timeout = Number(schedule.timeout) || schedule.timeout;
      item.silent = schedule.silent;
    }
    return item;
  });
  if (normalized.length === 0) return 'schedules: []\n';
  const lines = ['schedules:'];
  normalized.forEach((item) => {
    const entries = Object.entries(item);
    entries.forEach(([key, value], index) => {
      if (index === 0) lines.push(`- ${key}: ${formatScalar(value)}`);
      else lines.push(...writeValue(key, value, 2));
    });
  });
  return `${lines.join('\n')}\n`;
}

export function parseMcpClients(raw: string): McpClientFormState[] {
  const lines = splitLines(raw);
  const start = lines.findIndex((line) => /^clients:\s*/.test(line.trimEnd()));
  if (start < 0) return [];
  const clientsMap = parseSimpleMap(lines, start + 1, 0).value;
  return Object.entries(clientsMap).filter(([, value]) => value && typeof value === 'object' && !Array.isArray(value)).map(([id, value]) => {
    const client = value as SimpleYamlObject;
    return {
      id,
      description: String(client.description || ''),
      enabled: client.enabled !== false,
      transport: client.transport === 'sse' ? 'sse' : client.transport === 'stdio' ? 'stdio' : 'streamable_http',
      command: String(client.command || ''),
      argsText: Array.isArray(client.args) ? client.args.map(String).join('\n') : '',
      envText: serializeLooseObject(client.env),
      url: String(client.url || ''),
      headersText: serializeLooseObject(client.headers),
    };
  });
}

export function serializeMcpClients(clients: McpClientFormState[]): string {
  const lines = ['version: 1', 'clients:'];
  if (clients.length === 0) return 'version: 1\nclients: {}\n';
  clients.forEach((client) => {
    lines.push(`  ${client.id}:`);
    lines.push(`    transport: ${client.transport}`);
    lines.push(`    enabled: ${client.enabled ? 'true' : 'false'}`);
    lines.push(`    description: ${formatScalar(client.description)}`);
    if (client.transport === 'stdio') {
      lines.push(`    command: ${formatScalar(client.command)}`);
      const args = client.argsText.split('\n').map((item) => item.trim()).filter(Boolean);
      lines.push(...writeValue('args', args, 4));
      lines.push(...writeValue('env', parseLooseObject(client.envText), 4));
    } else {
      lines.push(`    url: ${formatScalar(client.url)}`);
      lines.push(...writeValue('headers', parseLooseObject(client.headersText), 4));
    }
  });
  return `${lines.join('\n')}\n`;
}

function serializeLooseObject(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  return Object.entries(value as Record<string, unknown>).map(([key, item]) => `${key}: ${String(item ?? '')}`).join('\n');
}

function parseLooseObject(text: string): SimpleYamlObject {
  const result: SimpleYamlObject = {};
  text.split('\n').forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const index = trimmed.indexOf(':');
    if (index < 0) return;
    const key = trimmed.slice(0, index).trim();
    const value = trimmed.slice(index + 1).trim();
    if (key) result[key] = parseScalar(value) as SimpleYamlValue;
  });
  return result;
}

export function parseSkillMarkdown(raw: string, fallbackName: string): SkillFormState {
  let meta: SimpleYamlObject = {};
  let body = raw;
  if (raw.startsWith('---\n')) {
    const end = raw.indexOf('\n---\n', 4);
    if (end >= 0) {
      meta = parseSimpleMap(raw.slice(4, end).split('\n'), 0, -1).value;
      body = raw.slice(end + 5);
    }
  }
  const keywords = Array.isArray(meta.keywords) ? meta.keywords.map(String) : [];
  return {
    name: String(meta.name || fallbackName),
    description: String(meta.description || ''),
    enabled: meta.enabled !== false,
    strategy: meta.strategy === 'constant' ? 'constant' : 'normal',
    keywordsText: keywords.join('\n'),
    order: meta.order === undefined ? '0' : String(meta.order),
    priority: meta.priority === undefined ? '0' : String(meta.priority),
    scan_depth: meta.scan_depth === undefined ? '0' : String(meta.scan_depth),
    body,
  };
}

export function serializeSkillMarkdown(form: SkillFormState): string {
  const keywords = form.keywordsText.split('\n').map((item) => item.trim()).filter(Boolean);
  const meta: SimpleYamlObject = {
    name: form.name,
    description: form.description,
    enabled: form.enabled,
    strategy: form.strategy,
    keywords,
    order: Number(form.order) || 0,
    priority: Number(form.priority) || 0,
    scan_depth: Number(form.scan_depth) || 0,
  };
  const frontmatter = Object.entries(meta).flatMap(([key, value]) => writeValue(key, value, 0)).join('\n');
  return `---\n${frontmatter}\n---\n\n${form.body.replace(/^\n+/, '')}`;
}
