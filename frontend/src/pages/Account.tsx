import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api'
import { useAuth } from '../auth'
import { Icon } from '../components/Icon'
import { ConfirmDialog, Dialog, ErrorNotice, Field, Loading, Notice, PasswordInput } from '../components/ui'
import { fmtRelative, fmtTime, setDisplayTimezone } from '../format'
import { useResource } from '../hooks'
import { useToast } from '../toast'
import { createPasskey, describeWebAuthnError, passkeysSupported } from '../webauthn'

function Passkeys() {
  const { me, refresh } = useAuth()
  const toast = useToast()
  const [name, setName] = useState(() => (/iPhone|iPad|Mac/.test(navigator.userAgent) ? 'Apple device' : /Android/.test(navigator.userAgent) ? 'Android device' : /Windows/.test(navigator.userAgent) ? 'Windows Hello' : 'Passkey'))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [remove, setRemove] = useState<any>(null)
  const [renaming, setRenaming] = useState<any>(null)
  const [newName, setNewName] = useState('')
  const supported = passkeysSupported()
  const blocker = me?.passkey_blocker
  const originMismatch = me?.passkey_origin && me.passkey_origin !== location.origin

  async function create() {
    setBusy(true); setError(null)
    try {
      const begin = await api('/api/auth/passkeys/register/begin', { body: { origin: location.origin } })
      const credential = await createPasskey(begin.options)
      await api('/api/auth/passkeys/register/finish', { body: { challenge_id: begin.challenge_id, credential, name, origin: location.origin } })
      toast('Passkey created')
      await refresh()
    } catch (e: any) { setError(e instanceof ApiError ? e.message : describeWebAuthnError(e)) } finally { setBusy(false) }
  }
  return (
    <section className="card">
      <h2>Passkeys</h2>
      <p className="muted">Sign in with your device’s screen lock or password manager instead of typing a password. Your password and recovery code keep working.</p>
      {!supported ? <Notice kind="warn">This browser cannot create passkeys here{!window.isSecureContext ? ' because the page is not served over HTTPS' : ''}.</Notice> : null}
      {blocker ? <Notice kind="warn" title="Passkeys are not available yet">{blocker} Configure the public address under Access below.</Notice> : null}
      {originMismatch ? <Notice kind="warn">Passkeys are bound to <strong>{me?.passkey_origin}</strong>, but you opened {location.origin}. Use the configured address to create or use passkeys.</Notice> : null}
      {error ? <Notice kind="bad">{error}</Notice> : null}
      {me?.passkeys?.length ? (
        <ul className="feed">{me.passkeys.map((p: any) => (
          <li key={p.id} className="row" style={{ padding: '10px 0', justifyContent: 'space-between' }}>
            <span><Icon name="key" /> <strong>{p.name}</strong><br /><span className="subtle">Created {fmtTime(p.created_at)} · last used {p.last_used_at ? fmtRelative(p.last_used_at) : 'never'}{p.backed_up ? ' · synced' : ''}</span></span>
            <span className="row"><button className="btn btn-sm" onClick={() => { setRenaming(p); setNewName(p.name) }}>Rename</button><button className="btn btn-sm btn-danger" onClick={() => setRemove(p)}>Remove</button></span>
          </li>))}</ul>
      ) : <p className="subtle">No passkeys yet.</p>}
      {supported && !blocker && !originMismatch ? (
        <div className="row" style={{ alignItems: 'flex-end', marginTop: 8 }}>
          <Field label="Name for the new passkey" htmlFor="pk-name"><input id="pk-name" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} /></Field>
          <button className="btn btn-primary" onClick={create} disabled={busy || !name.trim()}>{busy ? <span className="spinner" aria-hidden /> : <Icon name="plus" />} Create a passkey</button>
        </div>
      ) : null}
      <p className="subtle" style={{ marginTop: 8 }}>Passkeys only work at the exact hostname they were created for. If you later change the address (for example from an IP to a domain), existing passkeys stop working there and you sign in with your password to create new ones.</p>
      <ConfirmDialog open={!!remove} title={`Remove passkey “${remove?.name}”?`} confirmLabel="Remove" danger busy={busy} onCancel={() => setRemove(null)}
        onConfirm={async () => { setBusy(true); try { await api(`/api/auth/passkeys/${remove.id}`, { method: 'DELETE' }); setRemove(null); toast('Passkey removed'); await refresh() } catch (e: any) { toast(e.message, 'bad') } finally { setBusy(false) } }}>
        <p>You can no longer sign in with it here. Also delete it from your password manager or device if you no longer need it.</p>
      </ConfirmDialog>
      <Dialog open={!!renaming} onClose={() => setRenaming(null)} title="Rename passkey" footer={<><button className="btn" onClick={() => setRenaming(null)}>Cancel</button><button className="btn btn-primary" disabled={!newName.trim()} onClick={async () => { try { await api(`/api/auth/passkeys/${renaming.id}`, { method: 'PATCH', body: { name: newName } }); setRenaming(null); await refresh() } catch (e: any) { toast(e.message, 'bad') } }}>Save</button></>}>
        <Field label="Name" htmlFor="pk-rename"><input id="pk-rename" value={newName} maxLength={64} onChange={(e) => setNewName(e.target.value)} /></Field>
      </Dialog>
    </section>
  )
}

function Sessions() {
  const r = useResource<any>('/api/auth/sessions')
  const { logout } = useAuth()
  const toast = useToast()
  const [confirmAll, setConfirmAll] = useState(false)
  return (
    <section className="card">
      <div className="card-head"><h2>Signed-in devices</h2><button className="btn btn-sm btn-danger" onClick={() => setConfirmAll(true)}>Sign out all devices</button></div>
      {r.error ? <ErrorNotice error={r.error} onRetry={() => r.reload()} what="load sessions" /> : null}
      {!r.data ? <Loading /> : (
        <ul className="feed">{r.data.sessions.map((s: any) => (
          <li key={s.id} className="row" style={{ padding: '10px 0', justifyContent: 'space-between' }}>
            <span><strong>{s.device_label}</strong>{s.current ? <span className="badge info" style={{ marginLeft: 8 }}>This device</span> : null}<br />
              <span className="subtle">Signed in {fmtTime(s.created_at)} via {s.auth_method} · active {fmtRelative(s.last_seen_at)} · {s.remember ? `remembered until ${fmtTime(s.expires_at)}` : `expires ${fmtRelative(s.expires_at).replace('in ', 'in ')}`}{s.ip ? ` · ${s.ip}` : ''}</span></span>
            <button className="btn btn-sm" onClick={async () => { try { const x = await api(`/api/auth/sessions/${s.id}`, { method: 'DELETE' }); if (x.current) logout(); else { toast('Signed out'); r.reload(true) } } catch (e: any) { toast(e.message, 'bad') } }}>{s.current ? 'Sign out' : 'Revoke'}</button>
          </li>))}</ul>
      )}
      <ConfirmDialog open={confirmAll} title="Sign out everywhere?" confirmLabel="Sign out all devices" danger onCancel={() => setConfirmAll(false)}
        onConfirm={async () => { try { await api('/api/auth/sessions/revoke-all', { method: 'POST', body: {} }) } catch { /* ignore */ } logout() }}>
        <p>Every browser, including this one, must sign in again. Remembered devices are forgotten.</p>
      </ConfirmDialog>
    </section>
  )
}

function PasswordSection() {
  const { me } = useAuth()
  const toast = useToast()
  const [cur, setCur] = useState('')
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [code, setCode] = useState<string | null>(null)
  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true); setError(null)
    try { const r = await api('/api/auth/password', { body: { current_password: cur, new_password: pw } }); toast(`Password changed; ${r.other_sessions_revoked} other session(s) signed out`); setCur(''); setPw(''); setPw2('') }
    catch (e: any) { setError(e.message) } finally { setBusy(false) }
  }
  return (
    <section className="card">
      <h2>Password and recovery</h2>
      <form onSubmit={submit} className="stack">
        <input type="text" name="username" autoComplete="username" value={me?.username || ''} readOnly hidden />
        {error ? <Notice kind="bad">{error}</Notice> : null}
        <Field label="Current password" htmlFor="cur-pw"><PasswordInput id="cur-pw" value={cur} onChange={setCur} autoComplete="current-password" /></Field>
        <Field label="New password" htmlFor="new-pw" help="At least 12 characters."><PasswordInput id="new-pw" value={pw} onChange={setPw} autoComplete="new-password" /></Field>
        <Field label="Confirm new password" htmlFor="new-pw2" error={pw2 && pw !== pw2 ? 'Passwords do not match.' : undefined}><PasswordInput id="new-pw2" value={pw2} onChange={setPw2} autoComplete="new-password" /></Field>
        <div><button className="btn btn-primary" disabled={busy || !cur || pw.length < 12 || pw !== pw2}>Change password</button></div>
      </form>
      <hr className="divider" />
      <p>{me?.has_recovery_code ? 'You have an unused recovery code.' : 'You have no unused recovery code. Generate one now.'} Generating a new code invalidates the old one.</p>
      {code ? <><div className="code-box">{code}</div><p className="subtle">Shown once. Store it in your password manager.</p></> : null}
      <button className="btn" onClick={async () => { try { const r = await api('/api/auth/recovery-code/rotate', { method: 'POST', body: {} }); setCode(r.recovery_code) } catch (e: any) { toast(e.message, 'bad') } }}>Generate new recovery code</button>
    </section>
  )
}

function ProxySettings({ s, save }: { s: any; save: (body: any, msg: string) => Promise<void> }) {
  const p = s.proxy
  const [manual, setManual] = useState('')
  if (!p) return null
  const trustedAny = p.trusted_env.length + p.trusted_runtime.length > 0
  return (
    <fieldset>
      <legend>Reverse proxy</legend>
      <p className="muted" style={{ marginTop: 0 }}>
        {p.https_detected_by === 'proxy' ? 'You are connected through a trusted reverse proxy over HTTPS.'
          : p.https_detected_by === 'browser' ? 'HTTPS is detected from your browser. Everything works; trusting your proxy below additionally shows real device IPs in the session list and uses them for sign-in throttling.'
          : 'No reverse proxy is in use for this connection, or it is not trusted.'}
      </p>
      {p.detected_untrusted ? (
        <Notice kind="info" title="A reverse proxy was detected" actions={<button className="btn btn-sm btn-primary" onClick={() => save({ trust_proxy: p.detected_untrusted.address }, 'Proxy trusted')}>Trust {p.detected_untrusted.address}</button>}>
          <p>Requests arrive from <code>{p.detected_untrusted.address}</code> with proxy headers. Trust it if that is your reverse proxy (Caddy, Nginx, Synology). Only do this for a proxy you control.</p>
        </Notice>
      ) : null}
      <dl className="kv" style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 16px', margin: '8px 0' }}>
        <dt className="muted">This connection comes from</dt><dd style={{ margin: 0 }}><code>{p.connected_from}</code>{p.via_trusted_proxy ? ` (trusted proxy for ${p.client_ip})` : ''}</dd>
        <dt className="muted">Trusted proxies</dt><dd style={{ margin: 0 }}>
          {!trustedAny ? <span className="subtle">None</span> : null}
          {p.trusted_env.map((t: string) => <div key={t}><code>{t}</code> <span className="subtle">(TRUSTED_PROXIES)</span></div>)}
          {p.trusted_runtime.map((t: string) => <div key={t} className="row"><code>{t}</code><button className="btn btn-sm btn-ghost" onClick={() => save({ untrust_proxy: t }, 'Proxy removed')}>Remove</button></div>)}
        </dd>
      </dl>
      <details className="tech">
        <summary>Add a proxy address manually</summary>
        <div className="row" style={{ alignItems: 'flex-end', marginTop: 8 }}>
          <Field label="IP address or network" htmlFor="proxy-add"><input id="proxy-add" value={manual} placeholder="e.g. 172.31.0.0/16" onChange={(e) => setManual(e.target.value)} /></Field>
          <button className="btn" disabled={!manual.trim()} onClick={() => { save({ trust_proxy: manual.trim() }, 'Proxy trusted'); setManual('') }}>Trust</button>
        </div>
      </details>
    </fieldset>
  )
}

function AccessSettings() {
  const { refresh } = useAuth()
  const r = useResource<any>('/api/settings')
  const toast = useToast()
  const [origin, setOrigin] = useState<string | null>(null)
  const [tz, setTz] = useState<string | null>(null)
  const [key, setKey] = useState('')
  const zones: string[] = (Intl as any).supportedValuesOf ? (Intl as any).supportedValuesOf('timeZone') : ['UTC']
  async function save(body: any, msg: string) {
    try { const s = await api('/api/settings', { method: 'PUT', body }); r.setData(s); setDisplayTimezone(s.timezone); toast(msg); refresh() } catch (e: any) { toast(e.message, 'bad') }
  }
  if (!r.data) return <section className="card"><Loading /></section>
  const s = r.data
  return (
    <section className="card">
      <h2>Access and display</h2>
      <div className="stack">
        <Field label="Public address" htmlFor="origin" help={<>The exact URL you use to open this app, e.g. <code>https://webui.example.home</code>. Required for passkeys and used to check request origins. Currently opened at <code>{s.current_origin}</code>.</>}>
          <div className="row"><input id="origin" type="url" style={{ flex: '1 1 220px' }} value={origin ?? s.public_origin ?? ''} placeholder={s.current_origin} onChange={(e) => setOrigin(e.target.value)} />
            <button className="btn" onClick={() => save({ public_origin: origin ?? s.current_origin }, 'Public address saved')}>Save</button>
            {!s.public_origin ? <button className="btn btn-ghost" onClick={() => save({ public_origin: s.current_origin }, 'Public address saved')}>Use current</button> : null}</div>
        </Field>
        {s.insecure_transport ? <Notice kind="warn" title="This page is not using HTTPS">Open the web UI through your reverse proxy’s https:// address, or enable the built-in HTTPS option (TLS_SELF_SIGNED=true). Passkeys need HTTPS. See docs/https-and-passkeys.md.</Notice> : null}
        <ProxySettings s={s} save={save} />
        <Field label="Timezone for dates and day boundaries" htmlFor="tz">
          <div className="row"><select id="tz" style={{ flex: '1 1 220px' }} value={tz ?? s.timezone} onChange={(e) => setTz(e.target.value)}>{zones.map((z) => <option key={z}>{z}</option>)}</select>
            <button className="btn" onClick={() => save({ timezone: tz ?? s.timezone }, 'Timezone saved')}>Save</button>
            <button className="btn btn-ghost" onClick={() => { const b = Intl.DateTimeFormat().resolvedOptions().timeZone; setTz(b); save({ timezone: b }, 'Timezone saved') }}>Use this browser’s</button></div>
        </Field>
        <Field label="YouTube Data API key for video titles (optional)" htmlFor="ytkey" help={<>Titles and channel names are looked up without a key using YouTube’s public oEmbed endpoint. A key adds reliable channel IDs. {s.youtube_api_key_set ? `A key is set (${s.youtube_api_key_source}).` : 'No key set.'}</>}>
          <div className="row"><div style={{ flex: '1 1 220px' }}><PasswordInput id="ytkey" value={key} onChange={setKey} autoComplete="off" /></div>
            <button className="btn" disabled={!key} onClick={() => { save({ youtube_api_key: key }, 'API key saved'); setKey('') }}>Save</button>
            {s.youtube_api_key_source === 'runtime' ? <button className="btn btn-ghost" onClick={() => save({ clear_youtube_api_key: true }, 'API key removed')}>Remove</button> : null}</div>
        </Field>
      </div>
    </section>
  )
}

export default function AccountPage() {
  const { me } = useAuth()
  return (
    <div>
      <div className="page-head"><div><h1>Account &amp; security</h1><p>Signed in as <strong>{me?.username}</strong>{me?.session.remember ? ' · this device is remembered' : ''}.</p></div></div>
      <div className="stack" style={{ gap: 16 }}>
        <Passkeys />
        <Sessions />
        <PasswordSection />
        <AccessSettings />
      </div>
    </div>
  )
}
