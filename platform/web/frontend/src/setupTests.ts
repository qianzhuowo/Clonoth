// This setup file is added to make Vitest understand DOM assertions from Testing Library.
// It changes the test runtime by loading jest-dom once and cleaning rendered DOM after each test.
// The cleanup hook prevents one React render from leaking into the next UI test while the Supervisor service remains mocked.
// The purpose is to verify the React skeleton without connecting to a browser or the real Supervisor service.
import { cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
});
