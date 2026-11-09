import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, setReauthHandler } from '../api'
import { useAuth } from '../auth'
import { Dialog, Field, Notice, PasswordInput } from './ui'

/** Global "confirm your password" dialog used whenever the server answers reauth_required. */
export function ReauthHost() {
  const { me } = useAuth()
  const [open, setOpen] = useState(false)
  const [pw, setPw] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const resolver = useRef<((ok: boolean) => void) | null>(null)
  useEffect(() => {
    setReauthHandler(() => new Promise<boolean>((resolve) => { resolver.current = resolve; setPw(''); setError(null); setOpen(true) }))
    return () => setReauthHandler(null)
  }, [])
  const close = (ok: boolean) => { setOpen(false); setPw(''); resolver.current?.(ok); resolver.current = null }
  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true); setError(null)
    try { await api('/api/auth/reauth', { body: { password: pw }, retryReauth: false }); close(true) }
    catch (e: any) { setError(e.message) } finally { setBusy(false) }
  }
  return (
    <Dialog open={open} onClose={() => close(false)} title="Confirm it’s you">
      <form onSubmit={submit} className="stack">
        <p>This is a sensitive change. Enter your password to continue.</p>
        {error ? <Notice kind="bad">{error}</Notice> : null}
        <input type="text" name="username" autoComplete="username" value={me?.username || ''} readOnly hidden />
        <Field label="Password" htmlFor="reauth-password"><PasswordInput id="reauth-password" value={pw} onChange={setPw} autoComplete="current-password" autoFocus /></Field>
        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={() => close(false)}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={busy || !pw}>{busy ? <span className="spinner" aria-hidden /> : null} Confirm</button>
        </div>
      </form>
    </Dialog>
  )
}
