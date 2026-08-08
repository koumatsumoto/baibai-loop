import { resolve } from 'node:path'

import { defineConfig } from 'vitest/config'

// Tests are pure logic plus component checks rendered to static markup via
// react-dom/server, so no DOM environment or Vite plugins are needed. The `@` alias
// mirrors the app build so component modules resolve here.
export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
  },
  resolve: {
    alias: {
      '@': resolve(import.meta.dirname, './src'),
    },
  },
})
