// This common component barrel is added to keep shared UI controls easy to import.
// [2026-06-01] It now also exports the Material Symbols icon primitive. Why: old
// emoji glyphs are being replaced across the UI. How: expose Icon beside Button.
// Purpose: callers use a stable common import path for shared visual primitives.
export { Button } from './Button';
export { Icon } from './Icon';

// [2026-06-02] Export the raw settings editor from the common barrel.
// Why: System, Agents, Tools, Skills, MCP, Automation, and Advanced pages all need
// the same textarea editor. How: re-export the component and its lightweight YAML
// helper. Purpose: page imports stay short and consistent.
export { YamlEditor, hasSuspiciousYamlTabs } from './YamlEditor';
