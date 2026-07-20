import { defineConfig } from 'vitest/config'

// Unit tests currently cover only the pure value formatters in src/lib/format.ts, so
// the run needs no DOM environment or Vite plugins.
export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
  },
})
