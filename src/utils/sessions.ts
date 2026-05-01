import { genId } from './functions'

const id = genId()
const channel = new BroadcastChannel('sessions')

// Persisted set of session ids ever started by this browser profile.
// Used to distinguish "session belongs to this browser (alive or crashed)" from
// "session belongs to another device" — BroadcastChannel can't reach the latter,
// so a missed ping must NOT be interpreted as aborted in that case.
const STORAGE_KEY = 'aiaw-known-sessions'
const KNOWN_LIMIT = 100

function loadKnown(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) as string[] : []
  } catch {
    return []
  }
}
function recordKnown(sessionId: string) {
  try {
    const arr = loadKnown()
    if (arr.includes(sessionId)) return
    arr.push(sessionId)
    if (arr.length > KNOWN_LIMIT) arr.splice(0, arr.length - KNOWN_LIMIT)
    localStorage.setItem(STORAGE_KEY, JSON.stringify(arr))
  } catch { /* storage unavailable — best effort */ }
}
function isKnownLocally(sessionId: string): boolean {
  if (sessionId === id) return true
  return loadKnown().includes(sessionId)
}
recordKnown(id)

channel.addEventListener('message', ({ data }) => {
  if (data.type === 'ping') {
    channel.postMessage({ sessionId: id, type: 'pong' })
  }
})
function ping(sessionId: string) {
  return new Promise<boolean>(resolve => {
    if (sessionId === id) {
      resolve(true)
      return
    }
    channel.postMessage({ sessionId, type: 'ping' })
    const timeout = setTimeout(() => {
      channel.removeEventListener('message', listener)
      resolve(false)
    }, 50)
    const listener = ({ data }) => {
      if (data.type === 'pong' && data.sessionId === sessionId) {
        clearTimeout(timeout)
        resolve(true)
      }
    }
    channel.addEventListener('message', listener, { once: true })
  })
}

export default { id, ping, isKnownLocally }
