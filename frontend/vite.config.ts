/// <reference types="vitest" />
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

export default defineConfig(({ mode }) => ({
  /**
   * GitHub Pages serves this project from a subpath (`/osensa-demo/`), not the
   * domain root, so built asset URLs need that prefix or every request 404s.
   *
   * Keyed on `mode`, not `command`: `vite preview` runs with command "serve" but
   * mode "production", and it serves the already-built output whose asset URLs
   * already contain the prefix. Using `command` here would make preview serve from
   * `/` while the HTML asks for `/osensa-demo/...`, which 404s — and preview is
   * precisely where you would try to catch a base-path mistake.
   *
   * Deploying to a root-served host instead means changing this to '/'.
   */
  base: mode === 'production' ? '/osensa-demo/' : '/',
  plugins: [svelte()],
  server: { port: 5173 },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/lib/**/*.ts'],
      reporter: ['text', 'lcov'],
    },
  },
}));
