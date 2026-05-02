// Per-profile ports / shared backend / Postgres DSN. The defaults match
// what tests/scripts/run-playwright.sh + backend-start.sh configure.

export const BACKEND_URL = process.env.E2E_BACKEND_URL ?? 'http://127.0.0.1:9011'
export const BACKEND_WS_URL =
  process.env.E2E_BACKEND_WS_URL ?? 'ws://127.0.0.1:9011/api/v1/stream'
export const PG_DSN =
  process.env.E2E_PG_DSN ??
  'postgresql://aiaw:aiaw_test@127.0.0.1:5434/aiaw_test'

export const PROFILE_PORTS = {
  baseline: 9007,
  'providers-rest': 9008,
  'realtime-ws': 9009
} as const
export type ProfileName = keyof typeof PROFILE_PORTS

export function profileBaseURL(name: ProfileName): string {
  return `http://127.0.0.1:${PROFILE_PORTS[name]}`
}
