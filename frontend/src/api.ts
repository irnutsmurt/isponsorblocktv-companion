// Fetch wrapper: same-origin cookies, CSRF header, typed errors, auth/reauth signalling.

export class ApiError extends Error {
  status: number
  code?: string
  details?: any
  constructor(status: number, message: string, code?: string, details?: any) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
  }
}

type Listener = () => void
const unauthListeners = new Set<Listener>()
export function onUnauthorized(fn: Listener) { unauthListeners.add(fn); return () => { unauthListeners.delete(fn) } }

let reauthHandler: (() => Promise<boolean>) | null = null
export function setReauthHandler(fn: (() => Promise<boolean>) | null) { reauthHandler = fn }

function messageFrom(body: any, status: number): { message: string; code?: string; details?: any } {
  const d = body?.detail
  if (typeof d === 'string') return { message: d }
  if (d && typeof d === 'object') return { message: d.message || 'Request failed.', code: d.code, details: d }
  if (status === 422 && body?.errors) return { message: 'Some fields are invalid.', code: 'validation', details: body }
  if (status === 0) return { message: 'Network error' }
  return { message: `Request failed (${status}).` }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: any; signal?: AbortSignal; retryReauth?: boolean } = {}): Promise<T> {
  const method = opts.method || (opts.body !== undefined ? 'POST' : 'GET')
  let res: Response
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers: {
        'X-Requested-With': 'fetch',
        ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    })
  } catch (e: any) {
    if (e?.name === 'AbortError') throw e
    throw new ApiError(0, 'Cannot reach the companion. Check your connection; nothing was changed.', 'network')
  }
  if (res.status === 204) return undefined as T
  let body: any = null
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) {
    try { body = await res.json() } catch { body = null }
  }
  if (res.ok) return body as T
  const { message, code, details } = messageFrom(body, res.status)
  if (res.status === 401 && !path.startsWith('/api/auth/login') && !path.startsWith('/api/auth/reauth') && !path.startsWith('/api/auth/passkeys/login') && !path.startsWith('/api/auth/recover')) {
    unauthListeners.forEach((fn) => fn())
  }
  if (res.status === 403 && code === 'reauth_required' && reauthHandler && opts.retryReauth !== false) {
    const ok = await reauthHandler()
    if (ok) return api<T>(path, { ...opts, retryReauth: false })
  }
  throw new ApiError(res.status, message, code, details)
}

export async function download(path: string, fallbackName: string) {
  const res = await fetch(path, { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } })
  if (res.status === 403) {
    let body: any = null
    try { body = await res.json() } catch { /* ignore */ }
    if (body?.detail?.code === 'reauth_required' && reauthHandler && (await reauthHandler())) return download(path, fallbackName)
    throw new ApiError(403, body?.detail?.message || 'Not allowed.')
  }
  if (!res.ok) throw new ApiError(res.status, `Download failed (${res.status}).`)
  const blob = await res.blob()
  const cd = res.headers.get('content-disposition') || ''
  const m = /filename="([^"]+)"/.exec(cd)
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = m?.[1] || fallbackName
  document.body.appendChild(a)
  a.click()
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove() }, 1000)
}
