import { useCallback, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { CategoryBars, ColumnChart } from '../components/Charts'
import { Icon } from '../components/Icon'
import { CollectionBanner, DeviceStateBadge, RangePicker, rangeQuery, Stale, type Range } from '../components/Shared'
import { Empty, ErrorNotice, Loading, Notice } from '../components/ui'
import { catLabel, fmtDuration, fmtNumber, fmtRelative, fmtTime, KIND_LABELS } from '../format'
import { useDraft, useResource } from '../hooks'
import { subscribeLive } from '../live'

export function Metric({ label, value, note, state }: { label: string; value: string; note?: string; state?: 'ok' | 'notyet' | 'unavailable' }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{state === 'notyet' ? <span className="muted" style={{ fontSize: '1rem' }}>Not collected yet</span> : state === 'unavailable' ? <span className="muted" style={{ fontSize: '1rem' }}>Unavailable</span> : value}</div>
      {note ? <div className="note">{note}</div> : null}
    </div>
  )
}

export default function DashboardPage() {
  const [range, setRange] = useDraft<Range>('dash-range', { preset: '7d' })
  const q = rangeQuery(range)
  const ov = useResource<any>(`/api/stats/overview?${q}`, [q])
  const dev = useResource<any>(`/api/devices?${q}`, [q])
  const cfg = useResource<any>('/api/config')
  const debounce = useRef<number | null>(null)
  const refreshQuiet = useCallback(() => {
    if (debounce.current) window.clearTimeout(debounce.current)
    debounce.current = window.setTimeout(() => { ov.reload(true); dev.reload(true) }, 1500)
  }, [ov.reload, dev.reload])
  useEffect(() => subscribeLive((t) => { if (['event', 'device_state', 'enriched', 'collection', 'resumed', 'devices'].includes(t)) refreshQuiet() }), [refreshQuiet])

  const d = ov.data
  const col = d?.collection
  const notYet = !col?.coverage_start
  const unavailable = col?.status === 'disabled' && notYet
  const tileState = unavailable ? 'unavailable' : notYet ? 'notyet' : 'ok'
  const hourly = (d?.trend?.[0]?.bucket || '').includes('T')
  const fmtBucket = (b: string) => hourly ? b.slice(11, 16) : new Date(b + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  const noDevices = cfg.data && (!cfg.data.exists || (cfg.data.devices || []).length === 0)
  const coverageAfterStart = col?.coverage_start && d?.range?.start && col.coverage_start > d.range.start

  return (
    <div>
      <div className="page-head">
        <div><h1>Overview</h1><p>What iSponsorBlockTV has done on your TVs.</p></div>
        <div className="row"><Stale loadedAt={ov.loadedAt} /></div>
      </div>
      {noDevices ? (
        <Notice kind="info" title="Let’s connect your first TV" actions={<Link className="btn btn-primary" to="/getting-started">Start setup</Link>}>
          <p>{cfg.data?.exists ? 'iSponsorBlockTV has no devices configured yet.' : 'No iSponsorBlockTV configuration was found yet.'} The guided setup pairs a TV with its YouTube “Link with TV code”.</p>
        </Notice>
      ) : null}
      <CollectionBanner collection={col} />
      <div className="filters"><RangePicker value={range} onChange={setRange} /></div>
      {ov.error && !d ? <ErrorNotice error={ov.error} onRetry={() => ov.reload()} what="load statistics" /> : null}
      {ov.error && d ? <Notice kind="warn">Could not refresh; showing data loaded {fmtRelative(new Date(ov.loadedAt || Date.now()).toISOString())}.</Notice> : null}
      {!d && ov.loading ? <Loading /> : null}
      {d ? (
        <div className="stack" style={{ gap: 16 }}>
          {coverageAfterStart ? <Notice kind="info">Collection started {fmtTime(col.coverage_start)}. Earlier dates in this range have no data, not zero activity.</Notice> : null}
          <div className="tiles">
            <Metric state={tileState} label="Segment skips" value={fmtNumber(d.skips.count)} note={d.skips.pending ? `${d.skips.pending} still being matched to SponsorBlock` : 'requested by iSponsorBlockTV'} />
            <Metric state={tileState} label="Skipped segment length" value={fmtDuration(d.skips.segment_duration_seconds)} note={d.skips.unattributed ? `SponsorBlock duration; ${d.skips.unattributed} skip(s) without a matched segment` : 'SponsorBlock-reported duration, not measured'} />
            <Metric state={tileState} label="YouTube ads skipped" value={fmtNumber(d.ad_skips.count)} note="“Skip Ad” requests" />
            <Metric state={tileState} label="YouTube ads muted" value={fmtNumber(d.ad_mutes.count)} note={`${fmtDuration(d.ad_mutes.muted_seconds)} muted (not time saved)`} />
            <Metric state={tileState} label="Videos played" value={fmtNumber(d.distinct_videos)} note={`${fmtNumber(d.playbacks)} playback session(s)`} />
          </div>
          <div className="grid grid-2">
            <section className="card" aria-labelledby="trend-h">
              <div className="card-head"><h2 id="trend-h">Segment skips over time</h2><span className="subtle">{d.timezone}</span></div>
              {notYet ? <Empty title="No data collected yet" icon="clock">Activity appears here once iSponsorBlockTV skips something.</Empty> : <ColumnChart data={d.trend} valueKey="skips" label="Segment skips" formatBucket={fmtBucket} />}
            </section>
            <section className="card" aria-labelledby="cat-h">
              <div className="card-head"><h2 id="cat-h">By category</h2></div>
              {d.categories.length === 0 ? <Empty title={notYet ? 'No data collected yet' : 'No segment skips in this period'} icon="skip" /> : <CategoryBars items={d.categories} />}
              {d.categories.some((c: any) => c.category === 'unknown' || c.category === 'pending') ? <p className="subtle" style={{ marginTop: 12 }}>“Category unknown” means the skip was logged but no matching SponsorBlock segment could be found (for example the segment was removed since).</p> : null}
            </section>
          </div>
          <div className="grid grid-2">
            <section className="card" aria-labelledby="dev-h">
              <div className="card-head"><h2 id="dev-h">Devices</h2><Link to="/devices">Manage</Link></div>
              {dev.data?.devices?.length ? (
                <ul className="feed">
                  {dev.data.devices.filter((x: any) => x.configured).map((x: any) => (
                    <li key={x.id}><Link to={`/devices/${x.id}`} className="feed-item" style={{ textDecoration: 'none' }}>
                      <span className="feed-icon"><Icon name="tv" /></span>
                      <span className="feed-main"><span className="feed-title">{x.display_name}</span><br /><span className="feed-meta">{fmtNumber(x.stats?.skips)} skips · {fmtNumber(x.stats?.ad_skips)} ad skips · last seen {fmtRelative(x.last_seen_at)}</span></span>
                      <span><DeviceStateBadge state={x.state} /></span>
                    </Link></li>
                  ))}
                </ul>
              ) : <Empty title="No devices yet" icon="tv"><Link to="/getting-started" className="btn btn-primary">Pair a TV</Link></Empty>}
            </section>
            <section className="card" aria-labelledby="recent-h">
              <div className="card-head"><h2 id="recent-h">Recent activity</h2><Link to="/activity">All activity</Link></div>
              {d.recent.length === 0 ? <Empty title="Nothing yet" icon="list" /> : (
                <ul className="feed">{d.recent.map((e: any) => (
                  <li key={e.id} className="feed-item" style={{ cursor: 'default' }}>
                    <span className={`feed-icon ${e.kind === 'skip' ? 'skip' : ''}`}><Icon name={e.kind === 'ad_mute' ? 'mute' : 'skip'} /></span>
                    <span className="feed-main"><span className="feed-title">{e.kind === 'skip' ? (e.categories?.length ? e.categories.map(catLabel).join(' + ') : 'Segment') : KIND_LABELS[e.kind]}{e.segment_duration ? ` · ${fmtDuration(e.segment_duration)}` : ''}</span><br />
                      <span className="feed-meta">{e.title || (e.video_id ? `Video ${e.video_id}` : 'Video unknown')} · {e.device_name || 'Unknown device'}</span></span>
                    <span className="feed-time">{fmtRelative(e.at)}</span>
                  </li>))}
                </ul>)}
            </section>
          </div>
          <section className="card" aria-labelledby="health-h">
            <div className="card-head"><h2 id="health-h">Collection</h2><Link to="/diagnostics">Diagnostics</Link></div>
            <dl className="kv" style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 16px', margin: 0 }}>
              <dt className="muted">Status</dt><dd style={{ margin: 0 }}>{col?.status === 'connected' ? 'Collecting' : col?.status} {col?.source_kind && col.source_kind !== 'none' ? `(${col.source_kind === 'docker' ? 'Docker logs' : 'log file'})` : ''}</dd>
              <dt className="muted">Coverage since</dt><dd style={{ margin: 0 }}>{col?.coverage_start ? fmtTime(col.coverage_start) : 'Not started'}</dd>
              <dt className="muted">Newest record</dt><dd style={{ margin: 0 }}>{col?.last_record_at ? fmtTime(col.last_record_at) : '—'}</dd>
              <dt className="muted">Last successful update</dt><dd style={{ margin: 0 }}>{col?.last_successful_update ? fmtRelative(col.last_successful_update) : '—'}</dd>
              <dt className="muted">Gaps</dt><dd style={{ margin: 0 }}>{col?.recent_gaps?.length ? `${col.recent_gaps.length} recorded (most recent: ${col.recent_gaps[0].reason.split(':')[0]}, ${fmtTime(col.recent_gaps[0].started_at)}${col.recent_gaps[0].ended_at ? ` – ${fmtTime(col.recent_gaps[0].ended_at)}` : ', ongoing'})` : 'None recorded'}</dd>
            </dl>
          </section>
        </div>
      ) : null}
    </div>
  )
}
