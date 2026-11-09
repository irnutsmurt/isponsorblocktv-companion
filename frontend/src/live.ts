// Single shared SSE connection with backoff, pause on hidden tabs, and resubscribe after suspension.
type Handler = (type: string, data: any) => void
const handlers = new Set<Handler>()
let es: EventSource | null = null
let retry = 1000
let timer: number | null = null
let status: 'connecting' | 'open' | 'closed' = 'closed'
const statusHandlers = new Set<(s: string) => void>()
const TYPES = ['hello', 'event', 'device_state', 'playback', 'collection', 'run', 'enriched', 'apply_job', 'devices', 'config_changed', 'availability', 'upstream_fatal', 'session_ended']

function setStatus(s: typeof status) { status = s; statusHandlers.forEach((h) => h(s)) }

function connect() {
  if (es || document.visibilityState === 'hidden' || handlers.size === 0) return
  setStatus('connecting')
  es = new EventSource('/api/stream', { withCredentials: true })
  es.onopen = () => { retry = 1000; setStatus('open') }
  for (const t of TYPES) {
    es.addEventListener(t, (ev) => {
      let data: any = null
      try { data = JSON.parse((ev as MessageEvent).data) } catch { /* ignore */ }
      handlers.forEach((h) => h(t, data))
      if (t === 'session_ended') { disconnect(); }
    })
  }
  es.onerror = () => {
    disconnect()
    setStatus('closed')
    if (timer) window.clearTimeout(timer)
    timer = window.setTimeout(connect, retry + Math.random() * 500)
    retry = Math.min(retry * 2, 30000)
  }
}
function disconnect() { if (es) { es.close(); es = null } }

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') { disconnect(); setStatus('closed') }
  else { retry = 1000; connect(); handlers.forEach((h) => h('resumed', null)) }
})
window.addEventListener('online', () => { retry = 1000; connect() })

export function subscribeLive(h: Handler) {
  handlers.add(h)
  connect()
  return () => { handlers.delete(h); if (handlers.size === 0) { disconnect(); setStatus('closed') } }
}
export function onLiveStatus(h: (s: string) => void) { statusHandlers.add(h); h(status); return () => { statusHandlers.delete(h) } }
export function stopLive() { disconnect(); handlers.clear(); setStatus('closed') }
