// Why: the Duties theme now owns global scrollbar styling, and regressions are easy to miss in component tests.
// How: read the global stylesheet as source text and assert both Firefox and WebKit/Blink scrollbar rules exist.
// Purpose: keep all scrollable panes aligned with the low-contrast Duties visual language without adding runtime code.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const stylesheetPath = resolve(__dirname, '../styles/index.css');
const stylesheet = readFileSync(stylesheetPath, 'utf8');

describe('global Duties styles', () => {
  it('defines cross-browser custom scrollbar styling', () => {
    expect(stylesheet).toContain('scrollbar-width: thin;');
    expect(stylesheet).toContain('scrollbar-color: var(--duties-scrollbar-thumb) var(--duties-scrollbar-track);');
    expect(stylesheet).toContain('::-webkit-scrollbar');
    expect(stylesheet).toContain('width: 0.45rem;');
    expect(stylesheet).toContain('::-webkit-scrollbar-thumb:hover');
    expect(stylesheet).toContain('background: var(--duties-scrollbar-thumb-hover);');
  });
});
