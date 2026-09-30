import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Unit/component tests only — the production build is configured in vite.config.ts.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
    restoreMocks: true,
  },
})
