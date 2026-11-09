import { useEffect, useState } from 'react'
import { api } from '../api'
import { subscribeLive } from '../live'
import { fmtTime } from '../format'
import { Icon } from './Icon'
import { Notice } from './ui'

const STEP_LABELS: Record<string, string> = { save: 'Configuration saved', restart: 'iSponsorBlockTV restarted', verify: 'Devices active in iSponsorBlockTV', apply: 'Apply' }

export function useApplyJob(initial: any | null) {
  const [job, setJob] = useState<any | null>(initial)
  useEffect(() => { setJob(initial) }, [initial?.id])
  useEffect(() => {
    if (!job?.id) return
    const unsub = subscribeLive((type, data) => {
      if (type === 'apply_job' && data?.id === job.id) setJob((j: any) => ({ ...j, ...data }))
      if (type === 'resumed') api(`/api/config/jobs/${job.id}`).then(setJob).catch(() => {})
    })
    const finished = ['applied', 'failed'].includes(job.status) || (job.finished_at && job.status !== 'manual_restart_required')
    let timer: number | undefined
    if (!finished) timer = window.setInterval(() => { api(`/api/config/jobs/${job.id}`).then(setJob).catch(() => {}) }, 5000)
    return () => { unsub(); if (timer) clearInterval(timer) }
  }, [job?.id, job?.status])
  return [job, setJob] as const
}

export function ApplyJobView({ job, onRetry, onRestartNow }: { job: any; onRetry?: () => void; onRestartNow?: () => void }) {
  if (!job) return null
  const err = (() => { try { return job.error ? JSON.parse(job.error) : null } catch { return { message: job.error } } })()
  const status: string = job.status
  const headline: Record<string, { kind: 'info' | 'good' | 'warn' | 'bad'; text: string }> = {
    pending: { kind: 'info', text: 'Saving…' },
    saved: { kind: job.finished_at ? 'warn' : 'info', text: job.finished_at ? 'Saved, not applied yet' : 'Saved. Applying…' },
    restarting: { kind: 'info', text: 'Restarting iSponsorBlockTV…' },
    verifying: { kind: 'info', text: 'Waiting for iSponsorBlockTV to load the devices…' },
    applied: { kind: 'good', text: 'Saved, applied and verified' },
    manual_restart_required: { kind: 'warn', text: 'Saved. Restart iSponsorBlockTV to apply' },
    failed: { kind: 'bad', text: job.steps?.find((s: any) => s.name === 'save')?.status === 'ok' ? 'Saved, but applying failed' : 'Not saved' },
  }
  const h = headline[status] || { kind: 'info', text: status }
  return (
    <Notice kind={h.kind} title={h.text} details={err && (err.errors || err.code) ? err : undefined}
      actions={<>
        {status === 'failed' && onRetry ? <button className="btn btn-sm" onClick={onRetry}><Icon name="refresh" /> Retry apply</button> : null}
        {status === 'saved' && job.finished_at && onRestartNow ? <button className="btn btn-sm" onClick={onRestartNow}>Apply now</button> : null}
      </>}>
      <ol className="steps" aria-label="Progress">
        {(job.steps || []).map((s: any) => (
          <li key={s.name}>
            <span className={`st-icon st-${s.status}`}>{s.status === 'running' ? <span className="spinner" aria-hidden /> : <Icon name={s.status === 'ok' ? 'check' : s.status === 'failed' ? 'x' : s.status === 'manual' ? 'alert' : 'clock'} />}</span>
            <span><strong>{s.name === 'restart' && s.status === 'manual' ? 'Restart iSponsorBlockTV (waiting for you)' : s.name === 'verify' && s.status === 'pending' ? 'Waiting to confirm the devices are active' : STEP_LABELS[s.name] || s.name}</strong> <span className="sr-only">({s.status})</span><br /><span className="muted">{s.detail}</span>{s.at ? <span className="subtle"> · {fmtTime(s.at, { date: false, seconds: true })}</span> : null}</span>
          </li>
        ))}
      </ol>
      {err?.message && status === 'failed' ? <p style={{ marginTop: 8 }}>{err.message}</p> : null}
      {status === 'manual_restart_required' ? <p style={{ marginTop: 8 }}>Open your NAS’s container manager and restart the <strong>iSponsorBlockTV</strong> container. This page notices the restart automatically and confirms when the devices are active.</p> : null}
    </Notice>
  )
}
