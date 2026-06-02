// [2026-06-02] Structured Settings parser tests.
// Why: Settings forms serialize operational YAML without a frontend YAML dependency.
// How: cover providers, schedules, MCP clients, and skill frontmatter round trips.
// Purpose: future UI edits do not silently corrupt the common config shapes.
import { describe, expect, it } from 'vitest';

import {
  parseMcpClients,
  parseProvidersFromRuntime,
  parseSchedules,
  parseSkillMarkdown,
  replaceProvidersInRuntime,
  serializeMcpClients,
  serializeSchedules,
  serializeSkillMarkdown,
} from '../components/settings/settingsStructuredConfig';

describe('settingsStructuredConfig', () => {
  it('replaces only the runtime providers block', () => {
    const raw = 'version: 1\nengine:\n  model: ""\nproviders:\n  openai:\n    timeout_sec: 600.0\nmeta:\n  x: 1\n';
    const parsed = parseProvidersFromRuntime(raw);
    expect(parsed.providers.openai.timeout_sec).toBe(600);

    const next = replaceProvidersInRuntime(raw, { openai: { timeout_sec: 120, base_url: 'https://example.test/v1' } });
    expect(next).toContain('providers:\n  openai:\n    timeout_sec: 120\n    base_url: https://example.test/v1');
    expect(next).toContain('engine:\n  model: ""');
    expect(next).toContain('meta:\n  x: 1');
  });

  it('round trips editable schedules', () => {
    const raw = 'schedules:\n- id: demo\n  cron: "*/5 * * * *"\n  type: script\n  command: python task.py\n  enabled: false\n  once: true\n  text: "运行脚本"\n';
    const schedules = parseSchedules(raw);
    expect(schedules).toHaveLength(1);
    expect(schedules[0].id).toBe('demo');
    expect(schedules[0].type).toBe('script');
    expect(schedules[0].enabled).toBe(false);

    schedules[0].enabled = true;
    const serialized = serializeSchedules(schedules);
    expect(serialized).toContain('- id: demo');
    expect(serialized).toContain('type: script');
    expect(serialized).toContain('enabled: true');
  });

  it('round trips MCP clients with stdio args and HTTP headers', () => {
    const raw = 'version: 1\nclients:\n  local:\n    transport: stdio\n    enabled: true\n    command: npx\n    args:\n    - -y\n    - server\n  remote:\n    transport: streamable_http\n    enabled: false\n    url: https://mcp.example.test\n    headers:\n      Authorization: Bearer token\n';
    const clients = parseMcpClients(raw);
    expect(clients.map((client) => client.id)).toEqual(['local', 'remote']);
    expect(clients[0].argsText).toContain('server');
    expect(clients[1].headersText).toContain('Authorization: Bearer token');

    const serialized = serializeMcpClients(clients);
    expect(serialized).toContain('local:');
    expect(serialized).toContain('transport: stdio');
    expect(serialized).toContain('remote:');
    expect(serialized).toContain('transport: streamable_http');
  });

  it('round trips skill frontmatter and body', () => {
    const raw = '---\nname: demo\ndescription: 技能\nenabled: true\nstrategy: normal\nkeywords: ["a", "b"]\norder: 1\npriority: 2\nscan_depth: 3\n---\n\n# Demo\n正文\n';
    const form = parseSkillMarkdown(raw, 'fallback');
    expect(form.name).toBe('demo');
    expect(form.keywordsText).toBe('a\nb');
    form.enabled = false;
    form.body = '# Demo\n更新正文\n';

    const serialized = serializeSkillMarkdown(form);
    expect(serialized).toContain('enabled: false');
    expect(serialized).toContain('keywords:\n  - a\n  - b');
    expect(serialized).toContain('# Demo\n更新正文');
  });
});
