import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api'
import { useDraft } from '../hooks'
import { ApplyJobView, useApplyJob } from './ApplyJob'
import { Icon } from './Icon'
import { Field, Notice } from './ui'

function formatCode(raw: string) {
  const digits = raw.replace(/\D/g, '').slice(0, 12)
  return digits.replace(/(\d{4})(?=\d)/g, '$1 ').trim()
}

export function PairWizard({ etag, applyCapability, replaceDevice, onDone, onCancel }: { etag: string | null; applyCapability: any; replaceDevice?: { id: number; name: string } | null; onDone?: () => void; onCancel?: () => void }) {
  const [step, setStep] = useState<'code' | 'name' | 'saving'>('code')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [paired, setPaired] = useState<any | null>(null)
  const [draft, setDraft, clearDraft] = useDraft('pair-name', { name: replaceDevice?.name || '', offset: 0 })
  const [jobInit, setJobInit] = useState<any | null>(null)
  const [job] = useApplyJob(jobInit)
  const digits = code.replace(/\D/g, '')

  async function verify(e: FormEvent) {
    e.preventDefault()
    if (busy || digits.length !== 12) return
    setBusy(true); setError(null)
    try {
      const r = await api('/api/pairing/verify', { body: { code: digits } })
      setPaired(r)
      if (!draft.name && r.screen_name) setDraft({ ...draft, name: r.screen_name })
      setStep('name')
    } catch (e: any) { setError(e) } finally { setBusy(false) }
  }

  async function save(apply: boolean) {
    if (busy) return
    setBusy(true); setError(null)
    try {
      const r = await api('/api/devices', { body: { ticket: paired.ticket, name: draft.name.trim(), offset: Number(draft.offset) || 0, apply, expected_etag: etag, replace_device_id: replaceDevice?.id ?? null } })
      setJobInit(r.job)
      setStep('saving')
      clearDraft()
    } catch (e: any) {
      setError(e)
      if (e.code === 'ticket_expired') { setStep('code'); setPaired(null) }
    } finally { setBusy(false) }
  }

  const finished = job && (['applied', 'failed'].includes(job.status) || (job.finished_at && job.status !== 'verifying'))

  return (
    <div className="stack">
      <ol className="steps" aria-label="Pairing steps">
        <li><span className={`st-icon ${paired ? 'st-ok' : 'st-running'}`}><Icon name={paired ? 'check' : 'tv'} /></span><span><strong>1. Paired with YouTube</strong>{paired ? <span className="muted"> — {paired.screen_name || 'TV found'}</span> : null}</span></li>
        <li><span className={`st-icon ${job?.steps?.find((s: any) => s.name === 'save')?.status === 'ok' ? 'st-ok' : 'st-pending'}`}><Icon name={job?.steps?.find((s: any) => s.name === 'save')?.status === 'ok' ? 'check' : 'clock'} /></span><span><strong>2. Configuration saved</strong></span></li>
        <li><span className={`st-icon ${job?.status === 'applied' ? 'st-ok' : 'st-pending'}`}><Icon name={job?.status === 'applied' ? 'check' : 'clock'} /></span><span><strong>3. Active in iSponsorBlockTV</strong></span></li>
      </ol>

      {error ? <Notice kind="bad" title={error.code === 'duplicate' ? 'Already configured' : error.code === 'conflict' ? 'Configuration changed elsewhere' : 'That didn’t work'} details={error.details}>
        <p>{error.message}</p>
        {error.code === 'conflict' ? <p>Close this dialog and open it again to load the latest configuration. Your device name is kept.</p> : null}
      </Notice> : null}

      {step === 'code' ? (
        <form onSubmit={verify} className="stack">
          <Notice kind="info" title="Find the code on your TV">
            <ol style={{ margin: 0, paddingLeft: 20 }}>
              <li>Open the YouTube app on the TV.</li>
              <li>Go to <strong>Settings</strong> (gear icon, bottom of the left menu).</li>
              <li>Choose <strong>Link with TV code</strong>. A 12-digit code appears (it expires after a few minutes).</li>
            </ol>
          </Notice>
          <Field label="TV code" htmlFor="tv-code" help="12 digits, shown on the TV as three groups.">
            <input id="tv-code" className="pairing-code-input" inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(formatCode(e.target.value))} placeholder="1234 5678 9012" aria-describedby="tv-code-help" />
          </Field>
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            {onCancel ? <button type="button" className="btn" onClick={onCancel}>Cancel</button> : null}
            <button type="submit" className="btn btn-primary" disabled={busy || digits.length !== 12}>{busy ? <span className="spinner" aria-hidden /> : null} Pair</button>
          </div>
        </form>
      ) : null}

      {step === 'name' && paired ? (
        <form onSubmit={(e) => { e.preventDefault(); save(true) }} className="stack">
          <Notice kind="good" title="YouTube recognised this TV">
            <p>{paired.screen_name ? <>YouTube calls it “{paired.screen_name}”. </> : null}
              {paired.availability === 'online' ? 'The YouTube app on it is online right now.' : paired.availability === 'offline' ? 'The YouTube app is currently closed; that is fine.' : null}</p>
          </Notice>
          {paired.duplicate_of && !replaceDevice ? <Notice kind="warn" title="This TV is already known">It is {paired.duplicate_of.configured ? 'already configured' : 'in the history'} as “{paired.duplicate_of.name}”. {paired.duplicate_of.configured ? 'Saving again is blocked to avoid duplicates; rename it on the Devices page instead.' : 'Saving re-adds it and keeps its history.'}</Notice> : null}
          {paired.legacy_format ? <Notice kind="warn">YouTube returned an old-format screen ID. It may stop working; re-pair later if skipping stops.</Notice> : null}
          <Field label="Device name" htmlFor="dev-name" help="Shown in this app and in iSponsorBlockTV’s configuration.">
            <input id="dev-name" value={draft.name} maxLength={80} onChange={(e) => setDraft({ ...draft, name: e.target.value })} required />
          </Field>
          <details>
            <summary style={{ minHeight: 44, display: 'flex', alignItems: 'center', cursor: 'pointer' }}>Advanced: skip timing offset</summary>
            <Field label="Offset (milliseconds)" htmlFor="dev-offset" help="Start skips slightly early to compensate for TV lag. Leave at 0 unless skips land late.">
              <input id="dev-offset" type="number" inputMode="numeric" min={-10000} max={10000} value={draft.offset} onChange={(e) => setDraft({ ...draft, offset: Number(e.target.value) })} />
            </Field>
          </details>
          {applyCapability?.mode === 'restart'
            ? <p className="muted">Saving restarts the iSponsorBlockTV container so it picks up the new device. Skipping pauses on all TVs for about 10–30 seconds.</p>
            : <Notice kind="warn" title="Restart needed">{applyCapability?.reason} After saving, restart the iSponsorBlockTV container from your NAS; this page then confirms the device is active.</Notice>}
          <div className="row" style={{ justifyContent: 'flex-end' }}>
            <button type="button" className="btn" onClick={() => { setStep('code'); setPaired(null); setCode('') }}>Use a different code</button>
            {applyCapability?.mode === 'restart' ? <button type="button" className="btn" disabled={busy || !draft.name.trim() || (paired.duplicate_of?.configured && !replaceDevice)} onClick={() => save(false)}>Save only</button> : null}
            <button type="submit" className="btn btn-primary" disabled={busy || !draft.name.trim() || (paired.duplicate_of?.configured && !replaceDevice)}>{busy ? <span className="spinner" aria-hidden /> : null} {applyCapability?.mode === 'restart' ? 'Save and apply' : 'Save'}</button>
          </div>
        </form>
      ) : null}

      {step === 'saving' && job ? (
        <>
          <ApplyJobView job={job} onRetry={async () => { try { const r = await api('/api/config/apply', { method: 'POST', body: {} }); setJobInit(r.job) } catch (e: any) { setError(e) } }}
            onRestartNow={async () => { try { const r = await api('/api/config/apply', { method: 'POST', body: {} }); setJobInit(r.job) } catch (e: any) { setError(e) } }} />
          {finished || job.status === 'manual_restart_required' ? <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn btn-primary" onClick={onDone}>Done</button></div> : null}
        </>
      ) : null}
    </div>
  )
}
