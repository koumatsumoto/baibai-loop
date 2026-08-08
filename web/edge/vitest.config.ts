import { cloudflarePool } from '@cloudflare/vitest-pool-workers'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    pool: cloudflarePool({
      wrangler: { configPath: './wrangler.jsonc' },
    }),
  },
})
