import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { ApplyJobView, useApplyJob } from '../components/ApplyJob'
import { ConfirmDialog, Dialog, ErrorNotice, Field, Loading, Notice, PasswordInput } from '../components/ui'
import { fmtRelative, fmtTime } from '../format'
import { useResource } from '../hooks'
import { subscribeLive } from '../live'

type Form = Record<string, any>

function formFromConfig(c: any): Form {
  const e = c.effective
  return {
    skip_categories: [...(e.skip_categories || [])], minimum_skip_length: e.minimum_skip_length, skip_count_tracking: !!e.skip_count_tracking,
    skip_ads: !!e.skip_ads, mute_ads: !!e.mute_ads, auto_play: !!e.auto_play, join_name: e.join_name, apikey: e.apikey ? '[REDACTED]' : '',
    channel_whitelist: (e.channel_whitelist || []).map((x: any) => ({ id: x.id || '', name: x.name || '' })),
    use_proxy: !!e.use_proxy, sponsorblock_api_url: e.sponsorblock_api_url,
    devices: (c.devices || []).filter((d: any) => !d.invalid).map((d: any) => ({ index: d.index, name: d.name || '', offset: d.offset ?? 0 })),
  }
}

export default function ConfigPage() {
  const cfg = useResource<any>('/api/config')
  const [form, setForm] = useState<Form | null>(null)
  const [baseEtag, setBaseEtag] = useState<string | null>(null)
  const [externalChange, setExternalChange] = useState(false)
  const [errors, setErrors] = useState<any[]>([])
  const [warnings, setWarnings] = useState<any[]>([])
  const [error, setError] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [jobInit, setJobInit] = useState<any>(null)
  const [job] = useApplyJob(jobInit)
  const [restore, setRestore] = useState<any>(null)
  const [backupView, setBackupView] = useState<any>(null)
  const backups = useResource<any>('/api/config/backups')

  // Restore an unsaved draft for the same file version; otherwise load from server.
  useEffect(() => {
    if (!cfg.data || !cfg.data.effective) return
    if (form && baseEtag === cfg.data.etag) return
    if (!form) {
      let draft: any = null
      try { draft = JSON.parse(sessionStorage.getItem('isbtv-draft:config') || 'null') } catch { /* ignore */ }
      if (draft && draft.etag === cfg.data.etag) { setForm(draft.form) } else { setForm(formFromConfig(cfg.data)) }
      setBaseEtag(cfg.data.etag)
    } else if (baseEtag !== cfg.data.etag) {
      setExternalChange(true)
    }
  }, [cfg.data])
  useEffect(() => { if (form && baseEtag) try { sessionStorage.setItem('isbtv-draft:config', JSON.stringify({ etag: baseEtag, form })) } catch { /* ignore */ } }, [form, baseEtag])
  useEffect(() => subscribeLive((t) => { if (t === 'config_changed' || t === 'resumed') cfg.reload(true) }), [cfg.reload])

  const original = useMemo(() => (cfg.data?.effective ? formFromConfig(cfg.data) : null), [cfg.data?.etag])
  const dirty = !!form && !!original && JSON.stringify(form) !== JSON.stringify(original) && baseEtag === cfg.data?.etag
  const set = (k: string, v: any) => setForm((f) => ({ ...(f as Form), [k]: v }))
  const fieldErr = (path: string) => errors.find((e) => e.path === path || e.path.startsWith(path + '['))?.message
  const cap = cfg.data?.apply_capability

  async function save(apply: boolean) {
    if (!form || busy) return
    setBusy(true); setError(null); setErrors([]); setWarnings([])
    try {
      const r = await api('/api/config', { method: 'PUT', body: { config: form, expected_etag: baseEtag, apply } })
      setWarnings(r.warnings || [])
      setJobInit(r.job)
      try { sessionStorage.removeItem('isbtv-draft:config') } catch { /* ignore */ }
      setTimeout(() => { cfg.reload(true); backups.reload(true) }, 1500)
    } catch (e: any) {
      if (e.details?.errors) setErrors(e.details.errors)
      if (e.code === 'conflict') setExternalChange(true)
      setError(e)
    } finally { setBusy(false) }
  }

  function reloadFromDisk() {
    if (!cfg.data) return
    setForm(formFromConfig(cfg.data)); setBaseEtag(cfg.data.etag); setExternalChange(false); setError(null); setErrors([])
  }

  if (cfg.error && !cfg.data) return <ErrorNotice error={cfg.error} onRetry={() => cfg.reload()} what="load the configuration" />
  if (!cfg.data) return <Loading lines={8} />
  const c = cfg.data

  return (
    <div>
      <div className="page-head"><div><h1>Configuration</h1><p>iSponsorBlockTV’s settings (<code className="wrap-anywhere">{c.path}</code>). Settings apply to every TV.</p></div></div>
      {!c.exists ? <Notice kind="info" title="No configuration file yet">Add your first TV on the Devices page; the webui creates a valid config.json with default settings when it saves.</Notice> : null}
      {c.parse_error ? <Notice kind="bad" title="config.json is not valid">{c.parse_error} The webui will not overwrite it. Restore a backup below or fix the file manually.</Notice> : null}
      {!c.writable ? <Notice kind="bad" title="Cannot write the configuration" details={c.directory}>{c.write_problem}</Notice> : null}
      {c.has_legacy_atvs ? <Notice kind="bad" title="Legacy “atvs” entry">iSponsorBlockTV 2.x refuses to start while this key exists. <button className="btn btn-sm" onClick={() => form && api('/api/config', { method: 'PUT', body: { config: { ...form, remove_legacy_atvs: true }, expected_etag: baseEtag, apply: true } }).then((r) => setJobInit(r.job)).catch(setError)}>Remove it and apply</button></Notice> : null}
      {c.validation && !c.validation.ok && !c.parse_error ? <Notice kind="warn" title="The file on disk has problems" details={c.validation.errors}>{c.validation.errors.map((e: any) => e.message).join(' ')}</Notice> : null}
      {c.unknown_keys?.length ? <Notice kind="info">Keys this webui doesn’t manage are preserved unchanged: {c.unknown_keys.join(', ')}.</Notice> : null}
      {externalChange ? <Notice kind="warn" title="config.json changed outside this page" actions={<button className="btn btn-sm" onClick={reloadFromDisk}>Load latest (discard my edits)</button>}>Saving is blocked until you load the latest version, so nobody’s changes are silently overwritten.</Notice> : null}
      {job ? <ApplyJobView job={job} onRetry={() => api('/api/config/apply', { method: 'POST', body: {} }).then((r) => setJobInit(r.job)).catch(setError)} onRestartNow={() => api('/api/config/apply', { method: 'POST', body: {} }).then((r) => setJobInit(r.job)).catch(setError)} /> : null}
      {!job && c.latest_job && ['saved', 'manual_restart_required', 'failed'].includes(c.latest_job.status) ? <ApplyJobView job={c.latest_job} onRetry={() => api('/api/config/apply', { method: 'POST', body: {} }).then((r) => setJobInit(r.job)).catch(setError)} onRestartNow={() => api('/api/config/apply', { method: 'POST', body: {} }).then((r) => setJobInit(r.job)).catch(setError)} /> : null}
      {error && error.code !== 'conflict' ? <ErrorNotice error={error} what="save" /> : null}
      {warnings.length ? <Notice kind="warn">{warnings.map((w) => w.message).join(' ')}</Notice> : null}

      {form && c.effective ? (
        <form className="stack" onSubmit={(e) => { e.preventDefault(); save(true) }} style={{ gap: 16 }}>
          <section className="card">
            <h2>Segments</h2>
            <fieldset>
              <legend>Categories to skip</legend>
              <div className="grid grid-3" style={{ gap: 0 }}>
                {c.categories.map((cat: any) => (
                  <label key={cat.value} className="check"><input type="checkbox" checked={form.skip_categories.includes(cat.value)}
                    onChange={(e) => set('skip_categories', e.target.checked ? [...form.skip_categories, cat.value] : form.skip_categories.filter((x: string) => x !== cat.value))} />{cat.label}</label>
                ))}
              </div>
              {form.skip_categories.length === 0 ? <p className="subtle">No categories selected: segment skipping is off.</p> : null}
            </fieldset>
            <div className="grid grid-2" style={{ marginTop: 12 }}>
              <Field label="Minimum segment length (seconds)" htmlFor="msl" help="Segments shorter than this are not skipped. 0 skips all." error={fieldErr('minimum_skip_length')}>
                <input id="msl" type="number" min={0} inputMode="numeric" value={form.minimum_skip_length} onChange={(e) => set('minimum_skip_length', e.target.value === '' ? '' : Number(e.target.value))} />
              </Field>
            </div>
            <label className="check"><input type="checkbox" checked={form.skip_count_tracking} onChange={(e) => set('skip_count_tracking', e.target.checked)} /><span>Report skips to SponsorBlock<span className="help">Sends only the segment ID so contributors know their submission helped.</span></span></label>
          </section>
          <section className="card">
            <h2>YouTube ads</h2>
            <label className="check"><input type="checkbox" checked={form.skip_ads} onChange={(e) => set('skip_ads', e.target.checked)} /><span>Skip ads when YouTube allows it<span className="help">Presses “Skip Ad” the moment it appears.</span></span></label>
            <label className="check"><input type="checkbox" checked={form.mute_ads} onChange={(e) => set('mute_ads', e.target.checked)} /><span>Mute ads<span className="help">Mutes the TV while an ad plays. Doesn’t work when audio goes to an AirPlay speaker.</span></span></label>
          </section>
          <section className="card">
            <h2>Devices</h2>
            <p className="muted">Add, re-pair or remove TVs on the Devices page. Names and offsets can be edited here too.</p>
            {form.devices.map((d: any, i: number) => (
              <div key={d.index} className="grid grid-2" style={{ alignItems: 'end', marginBottom: 8 }}>
                <Field label={`Name of TV ${i + 1}`} htmlFor={`dn-${i}`} error={fieldErr(`devices[${d.index}]`)}><input id={`dn-${i}`} value={d.name} maxLength={80} onChange={(e) => set('devices', form.devices.map((x: any, j: number) => j === i ? { ...x, name: e.target.value } : x))} /></Field>
                <Field label="Offset (ms)" htmlFor={`do-${i}`}><input id={`do-${i}`} type="number" inputMode="numeric" value={d.offset} onChange={(e) => set('devices', form.devices.map((x: any, j: number) => j === i ? { ...x, offset: Number(e.target.value) } : x))} /></Field>
              </div>
            ))}
          </section>
          <section className="card">
            <h2>Channel whitelist</h2>
            <p className="muted">Videos from these channels are never skipped. Requires a YouTube Data API key; iSponsorBlockTV will not start with a whitelist and no key.</p>
            <Field label="YouTube Data API key" htmlFor="apikey" error={fieldErr('apikey') || fieldErr('channel_whitelist')} help={form.apikey === '[REDACTED]' ? 'A key is saved. Type a new one to replace it, or clear the field to remove it.' : undefined}>
              <PasswordInput id="apikey" value={form.apikey === '[REDACTED]' ? '' : form.apikey} onChange={(v) => set('apikey', v === '' && original?.apikey === '[REDACTED]' && form.apikey === '[REDACTED]' ? '[REDACTED]' : v)} autoComplete="off" />
            </Field>
            {form.apikey === '[REDACTED]' ? <button type="button" className="btn btn-sm btn-ghost" onClick={() => set('apikey', '')}>Remove saved key</button> : null}
            {form.channel_whitelist.map((ch: any, i: number) => (
              <div key={i} className="grid grid-2" style={{ alignItems: 'end', marginTop: 8 }}>
                <Field label="Channel ID" htmlFor={`cw-id-${i}`} help="Starts with UC…"><input id={`cw-id-${i}`} value={ch.id} onChange={(e) => set('channel_whitelist', form.channel_whitelist.map((x: any, j: number) => j === i ? { ...x, id: e.target.value.trim() } : x))} /></Field>
                <div className="row" style={{ alignItems: 'flex-end' }}>
                  <Field label="Label" htmlFor={`cw-n-${i}`}><input id={`cw-n-${i}`} value={ch.name} onChange={(e) => set('channel_whitelist', form.channel_whitelist.map((x: any, j: number) => j === i ? { ...x, name: e.target.value } : x))} /></Field>
                  <button type="button" className="btn btn-sm btn-danger" onClick={() => set('channel_whitelist', form.channel_whitelist.filter((_: any, j: number) => j !== i))}>Remove</button>
                </div>
              </div>
            ))}
            <button type="button" className="btn btn-sm" style={{ marginTop: 8 }} onClick={() => set('channel_whitelist', [...form.channel_whitelist, { id: '', name: '' }])}>Add channel</button>
          </section>
          <details className="card">
            <summary style={{ minHeight: 44, display: 'flex', alignItems: 'center', cursor: 'pointer', fontWeight: 650 }}>Advanced</summary>
            <label className="check"><input type="checkbox" checked={form.auto_play} onChange={(e) => set('auto_play', e.target.checked)} /><span>Keep YouTube autoplay enabled</span></label>
            <label className="check"><input type="checkbox" checked={form.use_proxy} onChange={(e) => set('use_proxy', e.target.checked)} /><span>Use the system proxy inside iSponsorBlockTV’s container</span></label>
            <div className="grid grid-2">
              <Field label="Name shown on the TV" htmlFor="jn" error={fieldErr('join_name')}><input id="jn" value={form.join_name} onChange={(e) => set('join_name', e.target.value)} /></Field>
              <Field label="SponsorBlock API URL" htmlFor="sburl" error={fieldErr('sponsorblock_api_url')}><input id="sburl" type="url" value={form.sponsorblock_api_url} onChange={(e) => set('sponsorblock_api_url', e.target.value)} /></Field>
            </div>
          </details>
          <div className="card" style={{ position: 'sticky', bottom: 'calc(var(--nav-h) * 0)', zIndex: 5 }}>
            <div className="row">
              <span className="muted" style={{ flex: '1 1 220px' }}>{dirty ? 'You have unsaved changes.' : 'No unsaved changes.'} {cap?.mode === 'restart' ? 'Applying restarts iSponsorBlockTV (≈10–30 s without skipping).' : cap?.reason}</span>
              <button type="button" className="btn" disabled={!dirty || busy} onClick={reloadFromDisk}>Discard</button>
              {cap?.mode === 'restart' ? <button type="button" className="btn" disabled={!dirty || busy || externalChange || !c.writable} onClick={() => save(false)}>Save only</button> : null}
              <button type="submit" className="btn btn-primary" disabled={!dirty || busy || externalChange || !c.writable}>{busy ? <span className="spinner" aria-hidden /> : null} {cap?.mode === 'restart' ? 'Save and apply' : 'Save'}</button>
            </div>
          </div>
        </form>
      ) : null}

      <section className="card" style={{ marginTop: 16 }}>
        <div className="card-head"><h2>Backups</h2><span className="subtle">A copy is kept before every change (newest {backups.data?.backups?.length ?? 0} retained)</span></div>
        {backups.data?.backups?.length ? (
          <ul className="feed">{backups.data.backups.map((b: any) => (
            <li key={b.id} className="row" style={{ padding: '8px 0', justifyContent: 'space-between' }}>
              <span>{fmtTime(b.created_at)} <span className="subtle">· before {b.reason} · {fmtRelative(b.created_at)}{b.actor ? ` · ${b.actor}` : ''}</span></span>
              <span className="row"><button className="btn btn-sm" onClick={() => api(`/api/config/backups/${b.id}`).then(setBackupView).catch(setError)} disabled={!b.available}>View</button>
                <button className="btn btn-sm" onClick={() => setRestore(b)} disabled={!b.available}>Restore</button></span>
            </li>))}</ul>
        ) : <p className="muted">No backups yet.</p>}
      </section>

      <Dialog open={!!backupView} onClose={() => setBackupView(null)} title="Backup contents">
        {backupView ? <>
          <p>{backupView.device_count} device(s). Screen IDs and API key are hidden.</p>
          {!backupView.validation.ok ? <Notice kind="warn">{backupView.validation.errors.map((e: any) => e.message).join(' ')}</Notice> : null}
          <pre className="mono" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: '.8rem' }}>{JSON.stringify(backupView.redacted, null, 2)}</pre>
        </> : null}
      </Dialog>
      <ConfirmDialog open={!!restore} title="Restore this backup?" confirmLabel={cap?.mode === 'restart' ? 'Restore and apply' : 'Restore'} danger busy={busy}
        onCancel={() => setRestore(null)} onConfirm={async () => {
          setBusy(true)
          try { const r = await api(`/api/config/backups/${restore.id}/restore?apply=${cap?.mode === 'restart'}`, { method: 'POST', body: {} }); setJobInit(r.job); setRestore(null); setForm(null); setTimeout(() => { cfg.reload(); backups.reload(true) }, 1500) } catch (e) { setError(e) } finally { setBusy(false) }
        }}>
        <p>The current config.json is backed up first, then replaced with the version from {restore ? fmtTime(restore.created_at) : ''}. TVs paired after that backup will be removed from iSponsorBlockTV, and screen IDs in the backup may have been revoked by YouTube since.</p>
      </ConfirmDialog>
    </div>
  )
}
