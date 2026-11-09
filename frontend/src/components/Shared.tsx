import { useEffect, useState } from 'react'
import { fmtRelative, fmtTime } from '../format'
import { Icon } from './Icon'
import { Badge, Field, Notice, Segmented } from './ui'

export type Range = { preset: string; start?: string; end?: string }

export function rangeQuery(r: Range) {
  const p = new URLSearchParams({ range: r.preset })
  if (r.preset === 'custom') { if (r.start) p.set('start', r.start); if (r.end) p.set('end', r.end) }
  return p.toString()
}

export function RangePicker({ value, onChange, allowAll }: { value: Range; onChange: (r: Range) => void; allowAll?: boolean }) {
  const opts = [{ value: 'today', label: 'Today' }, { value: '7d', label: '7 days' }, { value: '30d', label: '30 days' }, ...(allowAll ? [{ value: 'all', label: 'All' }] : []), { value: 'custom', label: 'Custom' }]
  return (
    <div className="row" style={{ alignItems: 'flex-end' }}>
      <Segmented label="Date range" value={value.preset} options={opts} onChange={(v) => onChange({ ...value, preset: v })} />
      {value.preset === 'custom' ? (
        <>
          <Field label="From" htmlFor="range-start"><input id="range-start" type="date" value={value.start || ''} onChange={(e) => onChange({ ...value, start: e.target.value })} /></Field>
          <Field label="To" htmlFor="range-end"><input id="range-end" type="date" value={value.end || ''} onChange={(e) => onChange({ ...value, end: e.target.value })} /></Field>
        </>
      ) : null}
    </div>
  )
}

const STATE_INFO: Record<string, { kind?: 'good' | 'warn' | 'bad' | 'info'; label: string; icon: string; explain: string }> = {
  playing: { kind: 'good', label: 'Playing', icon: 'play', explain: 'iSponsorBlockTV reported playback on this TV.' },
  connected: { kind: 'good', label: 'Connected', icon: 'plug', explain: 'iSponsorBlockTV is connected to this TV’s YouTube app.' },
  loaded: { kind: 'info', label: 'Monitoring', icon: 'check', explain: 'iSponsorBlockTV loaded this TV and is waiting for the YouTube app to be available.' },
  configured: { label: 'Configured', icon: 'tv', explain: 'In config.json; no activity from iSponsorBlockTV observed yet.' },
  disconnected: { kind: 'warn', label: 'Session ended', icon: 'unplug', explain: 'The TV’s YouTube session ended (app closed or TV off). iSponsorBlockTV reconnects automatically.' },
  unknown: { label: 'Unknown', icon: 'question', explain: 'Not enough evidence to say. Log silence alone does not mean the TV is off.' },
}
export function DeviceStateBadge({ state }: { state: string | null }) {
  const s = STATE_INFO[state || 'unknown'] || STATE_INFO.unknown
  return <Badge kind={s.kind} icon={s.icon}>{s.label}</Badge>
}
export const stateExplain = (state: string | null) => (STATE_INFO[state || 'unknown'] || STATE_INFO.unknown).explain

export function AvailabilityBadge({ value, at }: { value: string | null; at: string | null }) {
  if (!value) return <span className="subtle">Not checked</span>
  const map: Record<string, [any, string]> = { online: ['good', 'YouTube app online'], offline: [undefined, 'YouTube app offline'], unknown_screen: ['bad', 'Screen ID not recognised'], error: ['warn', 'Check failed'] }
  const [kind, label] = map[value] || [undefined, value]
  return <span><Badge kind={kind}>{label}</Badge> <span className="subtle">{fmtRelative(at)}</span></span>
}

export function CollectionBanner({ collection }: { collection: any }) {
  if (!collection) return null
  const st = collection.status
  if (st === 'connected') return null
  const lastUpd = collection.last_successful_update
  const title = st === 'disabled' ? 'Statistics collection is turned off' : st === 'starting' ? 'Connecting to iSponsorBlockTV…' : 'Statistics may be out of date'
  return (
    <Notice kind={st === 'starting' ? 'info' : 'warn'} title={title} details={collection.detail ? { status: st, code: collection.error_code, detail: collection.detail, since: collection.since } : undefined}>
      <p>{collection.detail || 'The companion cannot read iSponsorBlockTV’s activity right now.'} {lastUpd ? <>Showing data collected up to {fmtTime(collection.last_record_at || lastUpd)}.</> : null} Skipping on your TVs is not affected by this.</p>
    </Notice>
  )
}

export function Stale({ loadedAt }: { loadedAt: number | null }) {
  const [, tick] = useState(0)
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 30000); return () => clearInterval(t) }, [])
  if (!loadedAt) return null
  return <span className="subtle"><Icon name="clock" /> Updated {fmtRelative(new Date(loadedAt).toISOString())}</span>
}
