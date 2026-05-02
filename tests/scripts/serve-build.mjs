#!/usr/bin/env node
// Static-serve a quasar SPA build directory on 127.0.0.1:9007 (default) with
// SPA fallback (unknown extension-less paths → index.html). Used by
// playwright.config.ts as the e2e webServer. Plain node:http — no extra deps.
//
// Usage: node tests/scripts/serve-build.mjs --dir=<path> [--port=9007] [--host=127.0.0.1]
import http from 'node:http'
import { existsSync, statSync, createReadStream } from 'node:fs'
import path from 'node:path'

const args = process.argv.slice(2)
const getArg = (name, dflt) => {
  const a = args.find(x => x.startsWith(`--${name}=`))
  return a ? a.slice(`--${name}=`.length) : dflt
}

const dirArg = getArg('dir')
if (!dirArg) {
  process.stderr.write('usage: serve-build.mjs --dir=<path> [--port=9007] [--host=127.0.0.1]\n')
  process.exit(2)
}
const dir = path.resolve(dirArg)
const port = parseInt(getArg('port', '9007'), 10)
const host = getArg('host', '127.0.0.1')

if (!existsSync(path.join(dir, 'index.html'))) {
  process.stderr.write(`ERROR: ${dir}/index.html not found — is this a quasar SPA build dir?\n`)
  process.exit(1)
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.ico': 'image/x-icon',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.otf': 'font/otf',
  '.map': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.webmanifest': 'application/manifest+json'
}

const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent((req.url || '/').split('?')[0])
  const norm = path.posix.normalize(urlPath)
  let filePath = path.join(dir, norm)
  if (!filePath.startsWith(dir)) {
    res.writeHead(403)
    res.end('forbidden')
    return
  }

  if (existsSync(filePath) && statSync(filePath).isDirectory()) {
    filePath = path.join(filePath, 'index.html')
  }

  if (!existsSync(filePath)) {
    // SPA fallback: only for extensionless / unknown route paths
    if (path.extname(filePath) === '') {
      filePath = path.join(dir, 'index.html')
    } else {
      res.writeHead(404)
      res.end('not found')
      return
    }
  }

  const ext = path.extname(filePath).toLowerCase()
  const mime = MIME[ext] || 'application/octet-stream'
  res.writeHead(200, {
    'Content-Type': mime,
    'Cache-Control': 'no-cache'
  })
  createReadStream(filePath).pipe(res)
})

server.on('error', (e) => {
  if (e.code === 'EADDRINUSE') {
    process.stderr.write(`ERROR: ${host}:${port} already in use; refusing to start serve-build\n`)
    process.exit(1)
  }
  throw e
})

server.listen(port, host, () => {
  process.stderr.write(`[serve-build] http://${host}:${port} → ${dir}\n`)
})

const shutdown = () => {
  server.close()
  setTimeout(() => process.exit(0), 200).unref()
}
process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)
