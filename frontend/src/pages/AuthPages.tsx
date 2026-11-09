import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api'
import { useAuth } from '../auth'
import { Icon } from '../components/Icon'
import { Field, Notice, PasswordInput } from '../components/ui'
import { describeWebAuthnError, getPasskey, passkeysSupported } from '../webauthn'

function Brand() {
  return <div className="brand"><img src="/favicon.svg" alt="" /> iSponsorBlockTV Web UI</div>
}

function InsecureWarning() {
  if (window.isSecureContext && location.protocol === 'https:') return null
  if (location.hostname === 'localhost' || location.hostname === '127.0.0.1') return null
  return (
    <Notice kind="warn" title="This connection is not encrypted">
      <p>Your password travels over plain HTTP. Anyone on the same network could read it. Set up HTTPS (see the HTTPS guide in the documentation) before using this outside a trusted home network.</p>
    </Notice>
  )
}

export function SetupPage() {
  const { setupInfo, refresh } = useAuth()
  const [token, setToken] = useState('')
  const [username, setUsername] = useState('admin')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [recovery, setRecovery] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const mismatch = pw2.length > 0 && pw !== pw2
  const short = pw.length > 0 && pw.length < 12

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    if (pw !== pw2) { setError('The two passwords do not match.'); return }
    setBusy(true); setError(null)
    try {
      const r = await api('/api/auth/setup/complete', { body: { setup_token: token.trim(), username: username.trim(), password: pw } })
      setRecovery(r.recovery_code)
      setPw(''); setPw2(''); setToken('')
    } catch (e: any) {
      setError((e as ApiError).message)
    } finally { setBusy(false) }
  }

  if (recovery) {
    return (
      <div className="auth-wrap"><div className="card auth-card stack">
        <Brand />
        <h1>Save your recovery code</h1>
        <p>This single-use code is the only way back in if you forget your password. It is shown <strong>once</strong>. Store it in your password manager or print it.</p>
        <div className="code-box" aria-label="Recovery code">{recovery}</div>
        <div className="row">
          <button className="btn" onClick={() => navigator.clipboard?.writeText(recovery).catch(() => {})}>Copy code</button>
        </div>
        <label className="check"><input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} /> <span>I have stored the recovery code somewhere safe</span></label>
        <button className="btn btn-primary btn-block" disabled={!saved} onClick={() => refresh()}>Continue to setup</button>
      </div></div>
    )
  }

  return (
    <div className="auth-wrap"><form className="card auth-card stack" onSubmit={submit} noValidate>
      <Brand />
      <h1>Create the administrator</h1>
      <InsecureWarning />
      <p className="muted">To prove you own this installation, enter the setup token.
        {setupInfo?.token_source === 'env'
          ? ' It is the SETUP_TOKEN value in this container’s environment settings.'
          : ' Open your NAS’s container manager, view the logs of the webui container, and copy the token printed under “first-time setup”.'}
      </p>
      {setupInfo?.token_source === 'generated' && typeof setupInfo?.token_expires_in_seconds === 'number' ? (
        <p className="subtle">The current token expires in about {Math.max(1, Math.round(setupInfo.token_expires_in_seconds / 60))} minutes; a new one is printed when it does.</p>
      ) : null}
      {error ? <Notice kind="bad">{error}</Notice> : null}
      <Field label="Setup token" htmlFor="setup-token">
        <input id="setup-token" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" autoCapitalize="none" spellCheck={false} required />
      </Field>
      <Field label="Username" htmlFor="username">
        <input id="username" name="username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoCapitalize="none" required />
      </Field>
      <Field label="Password" htmlFor="new-password" help="At least 12 characters. A long passphrase is ideal; there is no upper limit you are likely to reach." error={short ? 'Use at least 12 characters.' : undefined}>
        <PasswordInput id="new-password" value={pw} onChange={setPw} autoComplete="new-password" invalid={short} describedBy="new-password-help" />
      </Field>
      <Field label="Confirm password" htmlFor="confirm-password" error={mismatch ? 'Passwords do not match.' : undefined}>
        <PasswordInput id="confirm-password" value={pw2} onChange={setPw2} autoComplete="new-password" invalid={mismatch} />
      </Field>
      <button className="btn btn-primary btn-block" type="submit" disabled={busy || !token || !username || pw.length < 12 || pw !== pw2}>
        {busy ? <span className="spinner" aria-hidden /> : null} Create account
      </button>
    </form></div>
  )
}

export function LoginPage() {
  const { refresh } = useAuth()
  const [mode, setMode] = useState<'login' | 'recover'>('login')
  const [username, setUsername] = useState('')
  const [pw, setPw] = useState('')
  const [remember, setRemember] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [newPw, setNewPw] = useState('')
  const [newRecovery, setNewRecovery] = useState<string | null>(null)

  async function login(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true); setError(null)
    try {
      await api('/api/auth/login', { body: { username: username.trim(), password: pw, remember } })
      setPw('')
      await refresh()
    } catch (e: any) {
      setError(e.status === 429 ? e.message : e.status === 0 ? e.message : e.status === 503 ? 'Sign-in is temporarily unavailable because the webui cannot read its account storage.' : 'Incorrect username or password.')
    } finally { setBusy(false) }
  }

  async function passkeyLogin() {
    setBusy(true); setError(null)
    try {
      const begin = await api('/api/auth/passkeys/login/begin', { body: { origin: location.origin } })
      const credential = await getPasskey(begin.options)
      await api('/api/auth/passkeys/login/finish', { body: { challenge_id: begin.challenge_id, credential, origin: location.origin, remember } })
      await refresh()
    } catch (e: any) {
      setError(e instanceof ApiError ? e.message : describeWebAuthnError(e))
    } finally { setBusy(false) }
  }

  async function recover(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true); setError(null)
    try {
      const r = await api('/api/auth/recover', { body: { username: username.trim(), recovery_code: code.trim(), new_password: newPw } })
      setNewRecovery(r.recovery_code)
      setNewPw(''); setCode('')
    } catch (e: any) { setError(e.message) } finally { setBusy(false) }
  }

  if (newRecovery) {
    return (
      <div className="auth-wrap"><div className="card auth-card stack">
        <Brand />
        <h1>Password reset</h1>
        <Notice kind="good">All other sessions were signed out. Here is your new single-use recovery code; the old one no longer works.</Notice>
        <div className="code-box">{newRecovery}</div>
        <button className="btn btn-primary btn-block" onClick={() => refresh()}>Continue</button>
      </div></div>
    )
  }

  if (mode === 'recover') {
    return (
      <div className="auth-wrap"><form className="card auth-card stack" onSubmit={recover}>
        <Brand />
        <h1>Recover your account</h1>
        <p className="muted">Use the recovery code you saved during setup. Every signed-in device will be signed out.</p>
        {error ? <Notice kind="bad">{error}</Notice> : null}
        <Field label="Username" htmlFor="r-username"><input id="r-username" name="username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoCapitalize="none" /></Field>
        <Field label="Recovery code" htmlFor="r-code"><input id="r-code" value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" autoCapitalize="characters" spellCheck={false} /></Field>
        <Field label="New password" htmlFor="r-new" help="At least 12 characters."><PasswordInput id="r-new" value={newPw} onChange={setNewPw} autoComplete="new-password" /></Field>
        <button className="btn btn-primary btn-block" type="submit" disabled={busy || !username || !code || newPw.length < 12}>{busy ? <span className="spinner" aria-hidden /> : null} Reset password</button>
        <button type="button" className="btn btn-ghost btn-block" onClick={() => { setMode('login'); setError(null) }}>Back to sign in</button>
      </form></div>
    )
  }

  return (
    <div className="auth-wrap"><form className="card auth-card stack" onSubmit={login} method="post" action="/api/auth/login">
      <Brand />
      <h1>Sign in</h1>
      <InsecureWarning />
      {error ? <Notice kind="bad">{error}</Notice> : null}
      <Field label="Username" htmlFor="username"><input id="username" name="username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username webauthn" autoCapitalize="none" autoCorrect="off" required autoFocus /></Field>
      <Field label="Password" htmlFor="current-password"><PasswordInput id="current-password" name="password" value={pw} onChange={setPw} autoComplete="current-password" /></Field>
      <label className="check"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
        <span>Remember this device for 30 days<span className="help">Stay signed in through browser and server restarts. Don’t use on shared devices.</span></span></label>
      <button className="btn btn-primary btn-block" type="submit" disabled={busy || !username || !pw}>{busy ? <span className="spinner" aria-hidden /> : null} Sign in</button>
      {passkeysSupported() ? (
        <button type="button" className="btn btn-block" onClick={passkeyLogin} disabled={busy}><Icon name="key" /> Sign in with a passkey</button>
      ) : null}
      <button type="button" className="btn btn-ghost btn-block" onClick={() => { setMode('recover'); setError(null) }}>Forgot password? Use your recovery code</button>
    </form></div>
  )
}
