import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { ApplyJobView, useApplyJob } from '../components/ApplyJob'
import { Icon } from '../components/Icon'
import { PairWizard } from '../components/PairWizard'
import { AvailabilityBadge, CollectionBanner, DeviceStateBadge, RangePicker, rangeQuery, stateExplain, type Range } from '../components/Shared'
import { ConfirmDialog, Dialog, Empty, ErrorNotice, Field, Loading, Notice } from '../components/ui'
import { catLabel, fmtDuration, fmtNumber, fmtRelative, fmtTime, KIND_LABELS } from '../format'
import { useDraft, useResource } from '../hooks'
import { subscribeLive } from '../live'
import { useToast } from '../toast'

function DeviceCard({ d, onRename, onRepair, onRemove, onCheck }: { d: any; onRename: () => void; onRepair: () => void; onRemove: () => void; onCheck: () => void }) {
  return (
    <article className="card device-card" aria-labelledby={`dev-${d.id}`}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <h3 id={`dev-${d.id}`}><Link to={`/devices/${d.id}`}>{d.display_name}</Link></h3>
        <DeviceStateBadge state={d.state} />
      </div>
      <p className="subtle" style={{ margin: 0 }}>{stateExplain(d.state)}{d.state_since ? ` Since ${fmtTime(d.state_since)}.` : ''}</p>
      {d.last_error ? <Notice kind="warn" title="Last problem reported">{d.last_error} <span className="subtle">({fmtRelative(d.last_error_at)})</span></Notice> : null}
      {d.legacy_screen_id ? <Notice kind="warn">Uses a legacy screen ID that YouTube is revoking. Re-pair this TV.</Notice> : null}
      <dl className="kv">
        <dt>Last seen</dt><dd>{d.last_seen_at ? fmtRelative(d.last_seen_at) : 'No activity recorded'}</dd>
        <dt>Now playing</dt><dd>{d.state === 'playing' && d.current_video_id ? (d.current_video_title || d.current_video_id) : '—'}</dd>
        <dt>YouTube status</dt><dd><AvailabilityBadge value={d.yt_availability} at={d.yt_availability_at} /></dd>
        <dt>Loaded by upstream</dt><dd>{d.loaded_in_current_run ? 'Yes, in the current run' : d.last_loaded_at ? `Last ${fmtRelative(d.last_loaded_at)}` : 'Not observed'}</dd>
        <dt>Skips (range)</dt><dd>{fmtNumber(d.stats?.skips)} · {fmtDuration(d.stats?.segment_duration_seconds)} of segments</dd>
        <dt>Ads (range)</dt><dd>{fmtNumber(d.stats?.ad_skips)} skipped · {fmtNumber(d.stats?.ad_mutes)} muted</dd>
      </dl>
      <div className="row">
        <button className="btn btn-sm" onClick={onRename}>Rename</button>
        <button className="btn btn-sm" onClick={onRepair}>Re-pair</button>
        <button className="btn btn-sm" onClick={onCheck}>Check status</button>
        <button className="btn btn-sm btn-danger" onClick={onRemove}>Remove</button>
      </div>
    </article>
  )
}

export default function DevicesPage() {
  const toast = useToast()
  const [range, setRange] = useDraft<Range>('dev-range', { preset: '7d' })
  const q = rangeQuery(range)
  const list = useResource<any>(`/api/devices?${q}`, [q])
  const cfg = useResource<any>('/api/config')
  const [pairOpen, setPairOpen] = useState<null | { replace?: { id: number; name: string } }>(null)
  const [rename, setRename] = useState<any | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [remove, setRemove] = useState<any | null>(null)
  const [forget, setForget] = useState<any | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<any>(null)
  const [jobInit, setJobInit] = useState<any>(null)
  const [job] = useApplyJob(jobInit)
  const reloadAll = useCallback(() => { list.reload(true); cfg.reload(true) }, [list.reload, cfg.reload])
  useEffect(() => subscribeLive((t) => { if (['device_state', 'devices', 'availability', 'config_changed', 'resumed', 'apply_job'].includes(t)) reloadAll() }), [reloadAll])

  async function mutate(path: string, body: any, method = 'POST') {
    setBusy(true); setError(null)
    try {
      await cfg.reload(true)
      const r = await api(path, { method, body: { ...body, expected_etag: cfg.data?.etag } })
      setJobInit(r.job)
      return true
    } catch (e: any) { setError(e); if (e.code === 'conflict') cfg.reload(true); return false } finally { setBusy(false) }
  }

  const configured = (list.data?.devices || []).filter((d: any) => d.configured)
  const removed = (list.data?.devices || []).filter((d: any) => !d.configured)
  const cap = cfg.data?.apply_capability
  const restartMode = cap?.mode === 'restart'

  return (
    <div>
      <div className="page-head">
        <div><h1>Devices</h1><p>TVs iSponsorBlockTV watches. Renaming keeps a device’s history.</p></div>
        <button className="btn btn-primary" onClick={() => setPairOpen({})} disabled={!!cfg.data?.parse_error || cfg.data?.writable === false}><Icon name="plus" /> Add a TV</button>
      </div>
      {cfg.data?.parse_error ? <Notice kind="bad" title="iSponsorBlockTV’s config.json cannot be read">{cfg.data.parse_error} Fix or restore it on the Configuration page before changing devices.</Notice> : null}
      {cfg.data && cfg.data.writable === false ? <Notice kind="bad" title="The webui cannot write the configuration">{cfg.data.write_problem}</Notice> : null}
      <CollectionBanner collection={list.data?.collection} />
      {error ? <ErrorNotice error={error} what="change the device" /> : null}
      {job ? <ApplyJobView job={job} onRetry={() => mutate('/api/config/apply', {})} onRestartNow={() => mutate('/api/config/apply', {})} /> : null}
      <div className="filters"><RangePicker value={range} onChange={setRange} /></div>
      {list.error && !list.data ? <ErrorNotice error={list.error} onRetry={() => list.reload()} what="load devices" /> : null}
      {!list.data && list.loading ? <Loading /> : null}
      {list.data && configured.length === 0 ? (
        <div className="card"><Empty title="No TVs configured" icon="tv"><p>Pair a TV with its YouTube “Link with TV code”.</p><button className="btn btn-primary" onClick={() => setPairOpen({})}>Add a TV</button></Empty></div>
      ) : null}
      <div className="device-grid">
        {configured.map((d: any) => (
          <DeviceCard key={d.id} d={d}
            onRename={() => { setRename(d); setRenameValue(d.config_name || d.display_name) }}
            onRepair={() => setPairOpen({ replace: { id: d.id, name: d.config_name || d.display_name } })}
            onRemove={() => setRemove(d)}
            onCheck={async () => { try { const r = await api(`/api/devices/${d.id}/check-availability`, { method: 'POST', body: {} }); toast(`YouTube reports: ${r.status.replace('_', ' ')}`); list.reload(true) } catch (e: any) { toast(e.message, 'bad') } }} />
        ))}
      </div>
      {removed.length ? (
        <section className="card" style={{ marginTop: 16 }}>
          <h2>Previously configured</h2>
          <p className="muted">History for these TVs is kept. They are no longer in iSponsorBlockTV’s configuration.</p>
          <ul className="feed">{removed.map((d: any) => (
            <li key={d.id} className="row" style={{ padding: '8px 0', justifyContent: 'space-between' }}>
              <span><Link to={`/devices/${d.id}`}>{d.display_name}</Link> <span className="subtle">removed {fmtRelative(d.removed_at)}</span></span>
              <button className="btn btn-sm btn-ghost" onClick={() => setForget(d)}>Delete history</button>
            </li>))}
          </ul>
        </section>
      ) : null}

      <Dialog open={!!pairOpen} onClose={() => { setPairOpen(null); reloadAll() }} title={pairOpen?.replace ? `Re-pair ${pairOpen.replace.name}` : 'Add a TV'}>
        {pairOpen ? <PairWizard etag={cfg.data?.etag ?? null} applyCapability={cap} replaceDevice={pairOpen.replace} onCancel={() => setPairOpen(null)} onDone={() => { setPairOpen(null); reloadAll() }} /> : null}
      </Dialog>

      <Dialog open={!!rename} onClose={() => setRename(null)} title="Rename device" footer={<>
        <button className="btn" onClick={() => setRename(null)}>Cancel</button>
        {restartMode ? <button className="btn" disabled={busy || !renameValue.trim()} onClick={async () => { if (await mutate(`/api/devices/${rename.id}`, { name: renameValue.trim(), apply: false }, 'PATCH')) setRename(null) }}>Save only</button> : null}
        <button className="btn btn-primary" disabled={busy || !renameValue.trim()} onClick={async () => { if (await mutate(`/api/devices/${rename.id}`, { name: renameValue.trim(), apply: true }, 'PATCH')) setRename(null) }}>{restartMode ? 'Save and apply' : 'Save'}</button>
      </>}>
        <Field label="Name" htmlFor="rename-input" help="History and statistics stay attached to this TV."><input id="rename-input" value={renameValue} maxLength={80} onChange={(e) => setRenameValue(e.target.value)} /></Field>
        <p className="subtle">{restartMode ? 'Applying restarts iSponsorBlockTV briefly. “Save only” takes effect at its next restart.' : cap?.reason}</p>
      </Dialog>

      <ConfirmDialog open={!!remove} title={`Remove ${remove?.display_name}?`} confirmLabel={restartMode ? 'Remove and apply' : 'Remove'} danger busy={busy}
        onCancel={() => setRemove(null)} onConfirm={async () => { if (await mutate(`/api/devices/${remove.id}/remove`, { apply: true })) setRemove(null) }}>
        <p>iSponsorBlockTV will stop skipping on this TV. Its activity history stays in the webui. To use it again you will need a new TV code.</p>
        {configured.length === 1 ? <Notice kind="bad">This is the last configured TV. iSponsorBlockTV refuses to start with no devices, so this change is blocked.</Notice> : null}
      </ConfirmDialog>

      <ConfirmDialog open={!!forget} title={`Delete history for ${forget?.display_name}?`} confirmLabel="Delete history" danger busy={busy}
        onCancel={() => setForget(null)} onConfirm={async () => { setBusy(true); try { await api(`/api/devices/${forget.id}/history`, { method: 'DELETE' }); setForget(null); toast('History deleted'); list.reload(true) } catch (e: any) { toast(e.message, 'bad') } finally { setBusy(false) } }}>
        <p>All recorded skips and activity for this TV are permanently deleted. This cannot be undone.</p>
      </ConfirmDialog>
    </div>
  )
}

export function DeviceDetailPage() {
  const { id } = useParams()
  const r = useResource<any>(`/api/devices/${id}`, [id])
  useEffect(() => subscribeLive((t, data) => { if ((['device_state', 'event', 'availability'].includes(t) && String(data?.device_id) === id) || t === 'resumed') r.reload(true) }), [id, r.reload])
  const d = r.data
  return (
    <div>
      <p><Link to="/devices">← Devices</Link></p>
      {r.error && !d ? <ErrorNotice error={r.error} onRetry={() => r.reload()} what="load this device" /> : null}
      {!d && r.loading ? <Loading /> : null}
      {d ? (
        <div className="stack" style={{ gap: 16 }}>
          <div className="page-head"><div><h1 className="wrap-anywhere">{d.display_name}</h1><p>{d.configured ? 'Configured in iSponsorBlockTV' : `Removed from configuration ${fmtRelative(d.removed_at)}`}{d.screen_name && d.screen_name !== d.display_name ? ` · YouTube name “${d.screen_name}”` : ''}</p></div><DeviceStateBadge state={d.state} /></div>
          <div className="tiles">
            <div className="tile"><div className="label">Segment skips (all time)</div><div className="value">{fmtNumber(d.stats.lifetime.skips)}</div><div className="note">{fmtDuration(d.stats.lifetime.segment_duration_seconds)} of SponsorBlock segments</div></div>
            <div className="tile"><div className="label">Ad skips</div><div className="value">{fmtNumber(d.stats.lifetime.ad_skips)}</div></div>
            <div className="tile"><div className="label">Ads muted</div><div className="value">{fmtNumber(d.stats.lifetime.ad_mutes)}</div><div className="note">{fmtDuration(d.stats.lifetime.muted_seconds)} muted</div></div>
            <div className="tile"><div className="label">First seen</div><div className="value" style={{ fontSize: '1rem' }}>{fmtTime(d.first_seen_at)}</div></div>
          </div>
          <section className="card">
            <h2>Status evidence</h2>
            <dl className="kv" style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 16px', margin: 0 }}>
              <dt className="muted">State</dt><dd style={{ margin: 0 }}>{stateExplain(d.state)}</dd>
              <dt className="muted">Evidence</dt><dd style={{ margin: 0 }}>{d.state_evidence || '—'} {d.state_since ? `(${fmtTime(d.state_since)})` : ''}</dd>
              <dt className="muted">Last connected</dt><dd style={{ margin: 0 }}>{fmtTime(d.last_connected_at)}</dd>
              <dt className="muted">Last session end</dt><dd style={{ margin: 0 }}>{fmtTime(d.last_disconnected_at)}</dd>
              <dt className="muted">Last log line</dt><dd style={{ margin: 0 }}>{fmtTime(d.last_log_at)}</dd>
              <dt className="muted">YouTube status</dt><dd style={{ margin: 0 }}><AvailabilityBadge value={d.yt_availability} at={d.yt_availability_at} /></dd>
              <dt className="muted">Last problem</dt><dd style={{ margin: 0 }}>{d.last_error ? `${d.last_error} (${fmtTime(d.last_error_at)})` : 'None recorded'}</dd>
              <dt className="muted">Screen ID</dt><dd style={{ margin: 0 }}>ends in …{d.screen_hint}{d.legacy_screen_id ? ' (legacy format)' : ''}</dd>
            </dl>
          </section>
          <section className="card">
            <div className="card-head"><h2>Recent activity</h2><Link to={`/activity?device_id=${d.id}`}>All activity for this TV</Link></div>
            {d.recent_activity.length === 0 ? <Empty title="No activity recorded" icon="list" /> : (
              <ul className="feed">{d.recent_activity.map((e: any) => (
                <li key={e.id} className="feed-item" style={{ cursor: 'default' }}>
                  <span className="feed-icon"><Icon name={e.kind === 'skip' || e.kind === 'ad_skip' ? 'skip' : e.kind === 'ad_mute' ? 'mute' : 'info'} /></span>
                  <span className="feed-main"><span className="feed-title">{e.kind === 'skip' && e.categories?.length ? e.categories.map(catLabel).join(' + ') : KIND_LABELS[e.kind] || e.kind}</span><br /><span className="feed-meta">{e.title || e.video_id || ''}</span></span>
                  <span className="feed-time">{fmtTime(e.at)}</span>
                </li>))}</ul>)}
          </section>
        </div>
      ) : null}
    </div>
  )
}

export { RangePicker }
