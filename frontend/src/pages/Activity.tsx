import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { Icon } from '../components/Icon'
import { RangePicker, rangeQuery, type Range } from '../components/Shared'
import { Empty, ErrorNotice, Field, Loading } from '../components/ui'
import { CATEGORY_LABELS, catLabel, fmtDuration, fmtTime, KIND_LABELS } from '../format'
import { useDraft, useResource } from '../hooks'
import { subscribeLive } from '../live'

type Filters = { range: Range; device_id: string; kind: string; category: string; search: string }

function iconFor(kind: string) {
  return kind === 'skip' || kind === 'ad_skip' ? 'skip' : kind === 'ad_mute' || kind === 'ad_unmute' ? 'mute' : kind === 'connected' ? 'plug' : kind === 'disconnected' ? 'unplug' : kind === 'error' || kind === 'warning' ? 'alert' : 'info'
}

function confidenceText(c: string | null) {
  return c === 'exact' ? 'Matched exactly to a SponsorBlock segment' : c === 'nearest' ? 'Matched to the nearest SponsorBlock segment (within 0.75 s)' : c === 'pending' ? 'Waiting for SponsorBlock lookup' : c === 'none' ? 'No matching SponsorBlock segment found' : c === 'unavailable' ? 'Video unknown when the skip was logged' : '—'
}

function EventDetail({ e }: { e: any }) {
  const ytUrl = e.video_id ? `https://www.youtube.com/watch?v=${encodeURIComponent(e.video_id)}${e.segment_start ? `&t=${Math.max(0, Math.floor(e.segment_start))}s` : ''}` : null
  return (
    <div className="feed-detail">
      <dl>
        <dt>When</dt><dd>{fmtTime(e.at, { date: true, seconds: true })}</dd>
        <dt>Device</dt><dd>{e.device_name || 'Unknown device'}</dd>
        <dt>Outcome</dt><dd>{e.kind === 'skip' || e.kind === 'ad_skip' ? 'Requested by iSponsorBlockTV (the TV does not report back whether it happened)' : e.kind === 'ad_mute' ? (e.duration != null ? `Muted for ${fmtDuration(e.duration)}` : 'Mute requested; no matching unmute was logged') : KIND_LABELS[e.kind] || e.kind}</dd>
        {e.video_id ? <><dt>Video</dt><dd>{ytUrl ? <a href={ytUrl} target="_blank" rel="noopener noreferrer">{e.title || e.video_id} <Icon name="external" /></a> : null}{!e.title ? <span className="subtle"> (title {e.meta_status === 'pending' ? 'not fetched yet' : e.meta_status === 'not_found' ? 'unavailable: private or removed video' : e.meta_status === 'disabled' ? 'lookup disabled' : 'unknown'})</span> : null}</dd></> : null}
        {e.channel_name || e.channel_url ? <><dt>Channel</dt><dd>{e.channel_url ? <a href={e.channel_url} target="_blank" rel="noopener noreferrer">{e.channel_name || 'Channel'} <Icon name="external" /></a> : e.channel_name}</dd></> : null}
        {e.kind === 'skip' ? <>
          <dt>Seeked to</dt><dd>{e.seek_to != null ? `${e.seek_to.toFixed(1)} s` : '—'}</dd>
          <dt>Segment</dt><dd>{e.segment_start != null ? `${e.segment_start.toFixed(1)} s → ${e.segment_end.toFixed(1)} s (${fmtDuration(e.segment_duration)})` : 'Unknown'}</dd>
          <dt>Category</dt><dd>{e.categories?.length ? e.categories.map(catLabel).join(', ') : 'Unknown'}</dd>
          <dt>Attribution</dt><dd>{confidenceText(e.match_confidence)}</dd>
        </> : null}
        {e.details?.message ? <><dt>Message</dt><dd className="mono">{e.details.message}</dd></> : null}
        {e.details?.hint ? <><dt>What to do</dt><dd>{e.details.hint}</dd></> : null}
      </dl>
    </div>
  )
}

export default function ActivityPage() {
  const [sp] = useSearchParams()
  const [f, setF] = useDraft<Filters>('activity-filters', { range: { preset: 'all' }, device_id: sp.get('device_id') || '', kind: '', category: '', search: '' })
  useEffect(() => { const d = sp.get('device_id'); if (d && d !== f.device_id) setF({ ...f, device_id: d }) }, [sp])
  const [searchInput, setSearchInput] = useState(f.search)
  useEffect(() => { const t = setTimeout(() => setF((p) => ({ ...p, search: searchInput })), 400); return () => clearTimeout(t) }, [searchInput])
  const devices = useResource<any>('/api/devices')
  const qs = new URLSearchParams(rangeQuery(f.range))
  if (f.device_id) qs.set('device_id', f.device_id)
  if (f.kind) qs.set('kind', f.kind)
  if (f.category) qs.set('category', f.category)
  if (f.search) qs.set('search', f.search)
  qs.set('limit', '50')
  const base = `/api/activity?${qs.toString()}`
  const first = useResource<any>(base, [base])
  const [items, setItems] = useState<any[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [moreLoading, setMoreLoading] = useState(false)
  const [moreError, setMoreError] = useState<any>(null)
  const [open, setOpen] = useState<Set<number>>(new Set())
  const [pending, setPending] = useState<any[]>([])
  const newestId = useRef<number>(0)

  useEffect(() => {
    if (first.data) { setItems(first.data.items); setHasMore(first.data.has_more); setPending([]); newestId.current = first.data.items[0]?.id || 0 }
  }, [first.data])

  const fetchNew = useCallback(async () => {
    if (!newestId.current && items.length) return
    try {
      const p = new URLSearchParams(qs); p.set('after_id', String(newestId.current)); p.set('limit', '100')
      const r = await api(`/api/activity?${p.toString()}`)
      if (r.items.length) {
        setPending((prev) => { const ids = new Set(prev.map((x) => x.id)); return [...r.items.filter((x: any) => !ids.has(x.id)), ...prev] })
        newestId.current = Math.max(newestId.current, ...r.items.map((x: any) => x.id))
      }
    } catch { /* stay quiet; next event retries */ }
  }, [base])
  useEffect(() => {
    let t: number | null = null
    return subscribeLive((type) => { if (type === 'event' || type === 'resumed') { if (t) clearTimeout(t); t = window.setTimeout(fetchNew, 800) } if (type === 'enriched') first.reload(true) })
  }, [fetchNew])

  async function loadMore() {
    if (!items.length) return
    setMoreLoading(true); setMoreError(null)
    try {
      const p = new URLSearchParams(qs); p.set('before_id', String(items[items.length - 1].id))
      const r = await api(`/api/activity?${p.toString()}`)
      setItems((x) => [...x, ...r.items]); setHasMore(r.has_more)
    } catch (e) { setMoreError(e) } finally { setMoreLoading(false) }
  }
  const toggle = (id: number) => setOpen((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })

  return (
    <div>
      <div className="page-head"><div><h1>Activity</h1><p>Everything iSponsorBlockTV reported, newest first.</p></div></div>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="filters" style={{ marginBottom: 8 }}>
          <Field label="Search" htmlFor="act-search"><input id="act-search" type="search" value={searchInput} onChange={(e) => setSearchInput(e.target.value)} placeholder="Title, channel or video ID" /></Field>
          <Field label="Device" htmlFor="act-dev"><select id="act-dev" value={f.device_id} onChange={(e) => setF({ ...f, device_id: e.target.value })}><option value="">All devices</option>{(devices.data?.devices || []).map((d: any) => <option key={d.id} value={d.id}>{d.display_name}{d.configured ? '' : ' (removed)'}</option>)}</select></Field>
          <Field label="Action" htmlFor="act-kind"><select id="act-kind" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>
            <option value="">All actions</option><option value="skip">Segment skips</option><option value="ad_skip">Ad skips</option><option value="ad_mute">Ad mutes</option><option value="connected,disconnected,loaded">Connections</option><option value="error,warning">Problems</option></select></Field>
          <Field label="Category" htmlFor="act-cat"><select id="act-cat" value={f.category} onChange={(e) => setF({ ...f, category: e.target.value })}><option value="">All categories</option>{Object.entries(CATEGORY_LABELS).filter(([k]) => k !== 'pending').map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
        </div>
        <RangePicker value={f.range} onChange={(r) => setF({ ...f, range: r })} allowAll />
      </div>
      {pending.length ? (
        <button className="btn btn-block" style={{ marginBottom: 12 }} onClick={() => { setItems((x) => [...pending, ...x]); setPending([]) }} aria-live="polite">
          <Icon name="refresh" /> {pending.length} new {pending.length === 1 ? 'entry' : 'entries'}: show
        </button>
      ) : null}
      {first.error && !first.data ? <ErrorNotice error={first.error} onRetry={() => first.reload()} what="load activity" /> : null}
      {!first.data && first.loading ? <Loading lines={6} /> : null}
      {first.data && items.length === 0 ? <div className="card"><Empty title="No matching activity" icon="list">{f.search || f.kind || f.category || f.device_id ? 'Try clearing some filters.' : 'Nothing has been recorded yet.'}</Empty></div> : null}
      {items.length ? (
        <div className="card">
          <ul className="feed">
            {items.map((e) => (
              <li key={e.id}>
                <button className="feed-item" aria-expanded={open.has(e.id)} onClick={() => toggle(e.id)}>
                  <span className={`feed-icon ${e.kind === 'skip' ? 'skip' : e.kind === 'error' ? 'bad' : e.kind === 'warning' ? 'warn' : ''}`}><Icon name={iconFor(e.kind)} /></span>
                  <span className="feed-main">
                    <span className="feed-title">{e.kind === 'skip' ? `${e.categories?.length ? e.categories.map(catLabel).join(' + ') : e.match_confidence === 'pending' ? 'Segment (matching…)' : 'Segment (category unknown)'}${e.segment_duration ? ` · ${fmtDuration(e.segment_duration)}` : ''}` : KIND_LABELS[e.kind] || e.kind}{e.kind === 'ad_mute' && e.duration != null ? ` · ${fmtDuration(e.duration)}` : ''}</span><br />
                    <span className="feed-meta">{e.video_id ? (e.title || `Video ${e.video_id}`) : e.details?.message || ''}{e.channel_name ? ` · ${e.channel_name}` : ''} · {e.device_name || 'Unknown device'}</span>
                  </span>
                  <span className="feed-time">{fmtTime(e.at)}</span>
                </button>
                {open.has(e.id) ? <EventDetail e={e} /> : null}
              </li>
            ))}
          </ul>
          {moreError ? <ErrorNotice error={moreError} onRetry={loadMore} what="load more" /> : null}
          {hasMore ? <button className="btn btn-block" style={{ marginTop: 12 }} onClick={loadMore} disabled={moreLoading}>{moreLoading ? <span className="spinner" aria-hidden /> : null} Load older</button> : <p className="subtle" style={{ textAlign: 'center', marginTop: 12 }}>End of history{first.data ? '' : ''}.</p>}
        </div>
      ) : null}
    </div>
  )
}
