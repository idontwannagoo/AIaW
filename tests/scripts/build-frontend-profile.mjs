#!/usr/bin/env node
// Build a flag-profile-specific quasar SPA bundle, with sha256-keyed cache.
//
// Why per-profile builds: process.env.* is inlined at build time by Quasar,
// so flipping flags at runtime is not possible. Each profile = one .env file
// = one cache slot. Cache key = sha256(env contents) + git rev + sha256(pkg).
//
// Cache hit  → prints existing build dir to stdout, exits 0 within ~50ms.
// Cache miss → temporarily swaps profile env into .env.local, runs
// `quasar build -m spa`, copies dist/spa to cache slot, restores .env.local.
//
// Trap-style restoration: original .env.local content is captured up front;
// SIGINT/SIGTERM/SIGHUP/uncaught/exit handlers all flow through one
// idempotent restore() so Ctrl+C mid-build never leaves a dirty .env.local.
//
// Usage:  node tests/scripts/build-frontend-profile.mjs --profile=<name>
// Stdout: absolute path to the cached build dir (one line, no trailing junk).
// Stderr: progress + cache hit/miss + build logs.
import { createHash } from 'node:crypto'
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { execFileSync, spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(__dirname, '..', '..')

const profile = (() => {
  const arg = process.argv.slice(2).find(a => a.startsWith('--profile='))
  if (!arg) {
    process.stderr.write('usage: build-frontend-profile.mjs --profile=<name>\n')
    process.exit(2)
  }
  const v = arg.slice('--profile='.length)
  if (!v) {
    process.stderr.write('--profile must have a value\n')
    process.exit(2)
  }
  return v
})()

const envFile = path.join(REPO_ROOT, 'tests', 'env', `.env.test.${profile}`)
if (!existsSync(envFile)) {
  process.stderr.write(`ERROR: profile env file not found: ${envFile}\n`)
  process.exit(1)
}

const sha = (buf) => createHash('sha256').update(buf).digest('hex')

const envContent = readFileSync(envFile)
const pkgContent = readFileSync(path.join(REPO_ROOT, 'package.json'))
const gitRev = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: REPO_ROOT }).toString().trim()

const cacheKey = sha(Buffer.concat([
  Buffer.from(`profile=${profile}\n`),
  Buffer.from(`env=${sha(envContent)}\n`),
  Buffer.from(`git=${gitRev}\n`),
  Buffer.from(`pkg=${sha(pkgContent)}\n`)
])).slice(0, 16)

const cacheDir = path.join(REPO_ROOT, 'tests', '.builds', profile, cacheKey)

if (existsSync(path.join(cacheDir, 'index.html'))) {
  process.stderr.write(`[build-frontend-profile] cache hit: ${profile}/${cacheKey}\n`)
  process.stdout.write(cacheDir + '\n')
  process.exit(0)
}

process.stderr.write(`[build-frontend-profile] cache miss → quasar build (profile=${profile}, key=${cacheKey})\n`)

const envLocal = path.join(REPO_ROOT, '.env.local')
const originalExisted = existsSync(envLocal)
const originalContent = originalExisted ? readFileSync(envLocal) : null

let restored = false
const restore = () => {
  if (restored) return
  restored = true
  try {
    if (originalContent !== null) {
      writeFileSync(envLocal, originalContent)
    } else if (existsSync(envLocal)) {
      rmSync(envLocal)
    }
  } catch (e) {
    process.stderr.write(`[build-frontend-profile] WARN: failed to restore .env.local: ${e.message}\n`)
  }
}

for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => {
    restore()
    process.exit(130)
  })
}
process.on('exit', restore)
process.on('uncaughtException', (e) => {
  restore()
  process.stderr.write(`[build-frontend-profile] uncaught: ${e.stack || e.message}\n`)
  process.exit(1)
})

try {
  writeFileSync(envLocal, envContent)

  const quasarBin = path.join(REPO_ROOT, 'node_modules', '.bin', 'quasar')
  if (!existsSync(quasarBin)) {
    throw new Error(`quasar binary not found at ${quasarBin}; run pnpm i first`)
  }

  const result = spawnSync(quasarBin, ['build', '-m', 'spa'], {
    cwd: REPO_ROOT,
    stdio: 'inherit',
    env: process.env
  })
  if (result.status !== 0) {
    throw new Error(`quasar build exited with code ${result.status}`)
  }

  const distDir = path.join(REPO_ROOT, 'dist', 'spa')
  if (!existsSync(distDir)) {
    throw new Error(`dist/spa not found after build`)
  }

  mkdirSync(path.dirname(cacheDir), { recursive: true })
  if (existsSync(cacheDir)) rmSync(cacheDir, { recursive: true, force: true })
  cpSync(distDir, cacheDir, { recursive: true })

  process.stderr.write(`[build-frontend-profile] cached: ${cacheDir}\n`)
  process.stdout.write(cacheDir + '\n')
} finally {
  restore()
}
