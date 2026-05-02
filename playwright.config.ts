// Playwright e2e config — Phase 4 of plans/test-infrastructure.md.
//
// Per-profile build dirs come in as E2E_BUILD_DIR_* env vars set by
// tests/scripts/run-playwright.sh. The shell wrapper builds each flag profile
// serially (quasar build writes to dist/spa, can't run three in parallel)
// then execs `pnpm exec playwright test`. webServer entries here only do
// the lightweight serve-build step.
//
// Why per-profile ports + per-profile webServer entries: process.env.* is
// inlined at build time, so each flag combination = a separate static bundle.
// Each project (baseline / providers-rest / realtime-ws) lives on its own
// sirv instance. The backend (9011) is shared across all three.
//
// Workers are pinned to 1: every test reuses the single backend on 9011 and
// would clash on TRUNCATE-style cleanup if parallelized. Stage-3+ when we add
// per-test cleanup helpers we may relax this.
import { defineConfig, devices } from '@playwright/test'

interface Profile {
  name: string
  port: number
  buildDir: string
}

function requireEnv(name: string): string {
  const v = process.env[name]
  if (!v) {
    throw new Error(
      `${name} not set. Run via tests/scripts/run-playwright.sh ` +
      '(invoked by `pnpm test:e2e`), not raw `playwright test`.'
    )
  }
  return v
}

const PROFILES: Profile[] = [
  { name: 'baseline', port: 9007, buildDir: requireEnv('E2E_BUILD_DIR_BASELINE') },
  { name: 'providers-rest', port: 9008, buildDir: requireEnv('E2E_BUILD_DIR_PROVIDERS_REST') },
  { name: 'realtime-ws', port: 9009, buildDir: requireEnv('E2E_BUILD_DIR_REALTIME_WS') },
  { name: 'realtime-sse', port: 9012, buildDir: requireEnv('E2E_BUILD_DIR_REALTIME_SSE') },
  { name: 'realtime-poll', port: 9013, buildDir: requireEnv('E2E_BUILD_DIR_REALTIME_POLL') },
  { name: 'realtime-auto', port: 9014, buildDir: requireEnv('E2E_BUILD_DIR_REALTIME_AUTO') }
]

export default defineConfig({
  testDir: './tests/e2e',
  outputDir: './tests/.results/e2e-output',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ['list'],
    ['json', { outputFile: 'tests/.results/e2e.json' }],
    ['html', { outputFolder: 'tests/.results/e2e-html', open: 'never' }]
  ],
  use: {
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure'
  },
  webServer: PROFILES.map(p => ({
    command: `node tests/scripts/serve-build.mjs --dir=${p.buildDir} --port=${p.port} --host=127.0.0.1`,
    url: `http://127.0.0.1:${p.port}/index.html`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    stdout: 'pipe',
    stderr: 'pipe'
  })),
  projects: PROFILES.map(p => ({
    name: p.name,
    testMatch: /.*\.spec\.ts/,
    use: {
      ...devices['Desktop Chrome'],
      baseURL: `http://127.0.0.1:${p.port}`
    },
    metadata: { profile: p.name, buildDir: p.buildDir }
  }))
})
