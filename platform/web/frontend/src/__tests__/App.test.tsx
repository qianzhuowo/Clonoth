// [2026-05-16] Updated: minimal smoke test for App rendering.
// [2026-05-31] Step 3 expectation: App must mount the reducer-backed V2 chat list
// and the bottom event log panel. Why: the integration switch should be protected at
// the application boundary. How: the smoke test now resets chatStoreV2 and asserts V2
// only UI text. Purpose: catch accidental fallback to the legacy MessageList path.
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import App from '../App';
import { useChatStoreV2 } from '../store/chatStoreV2';
import { useSettingsStore } from '../store/settingsStore';

describe('Clonoth web app', () => {
  beforeEach(() => {
    useChatStoreV2.getState().resetState();
    // [2026-05-17] The app now has an admin login gate. This smoke test is meant
    // to verify the main chat layout, so it explicitly enters the authenticated
    // state instead of accidentally testing the login page.
    useSettingsStore.getState().setAuthenticated(true);
    // [2026-05-31] App now calls startup/session config endpoints on mount. Why:
    // the smoke test should verify rendering, not depend on a live Supervisor.
    // How: return harmless JSON for every request. Purpose: keep this boundary test
    // deterministic while still exercising the mount effects.
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([]), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })));
  });

  afterEach(() => {
    useChatStoreV2.getState().resetState();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders the V2 app layout with the event log slot', () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: /Clonoth 网页端/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /新对话/i })).toBeInTheDocument();
    expect(screen.getByText(/请选择或创建一个对话/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/事件日志面板/i)).toBeInTheDocument();
  });
});
