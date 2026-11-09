import { Fragment, useState } from 'react'
import { api } from '../api'
import { Icon } from '../components/Icon'
import { RangePicker, rangeQuery, type Range } from '../components/Shared'
import { Empty, ErrorNotice, Field, Loading, Notice } from '../components/ui'
import { fmtDuration, fmtNumber, fmtRelative } from '../format'
import { useDraft, useResource } from '../hooks'

function ChannelName({ c }: { c: any }) {
  if (c.unknown) return <span className="muted">Channel not known yet</span>
  const name = c.channel_name || 'Unnamed channel'
  return c.channel_url ? <a href={c.channel_url} target="_blank" rel="noopener noreferrer">{name} <Icon name="external" /></a> : <>{name}</>
}

function Videos({ c, q }: { c: any; q: string }) {
  const r = useResource<any>(`/api/channels/${encodeURIComponent(c.channel_key)}/videos?${q}`, [c.channel_key, q])
  if (r.loading && !r.data) return <Loading lines={2} />
  if (r.error) return <ErrorNotice error={r.error} what="load videos" />
  return (
    <ul style={{ margin: 0, paddingLeft: 18 }}>
      {r.data.videos.map((v: any) => <li key={v.video_id}><a href={`https://www.youtube.com/watch?v=${encodeURIComponent(v.video_id)}`} target="_blank" rel="noopener noreferrer">{v.title || v.video_id}</a> <span className="subtle">· {v.skips} skip(s) · {fmtDuration(v.segment_duration_seconds)} · {fmtRelative(v.last_at)}</span></li>)}
    </ul>
  )
}

export default function ChannelsPage() {
  const [range, setRange] = useDraft<Range>('ch-range', { preset: '30d' })
  const [device, setDevice] = useDraft('ch-dev', '')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [open, setOpen] = useState<string | null>(null)
  const devices = useResource<any>('/api/devices')
  const rq = rangeQuery(range)
  const qs = new URLSearchParams(rq)
  if (device) qs.set('device_id', device)
  if (search) qs.set('search', search)
  qs.set('limit', '50'); qs.set('offset', String(offset))
  const r = useResource<any>(`/api/channels?${qs}`, [qs.toString()])
  const settings = useResource<any>('/api/settings')
  void api
  return (
    <div>
      <div className="page-head"><div><h1>Channels</h1><p>Channels whose videos had segments skipped. A skip says nothing about the channel as a whole.</p></div></div>
      {settings.data && !settings.data.enrichment_enabled ? <Notice kind="info">Video metadata lookup is disabled (ENRICHMENT_ENABLED=false), so channels cannot be identified.</Notice> : null}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="filters">
          <Field label="Search" htmlFor="ch-search"><input id="ch-search" type="search" value={search} onChange={(e) => { setSearch(e.target.value); setOffset(0) }} placeholder="Channel or video title" /></Field>
          <Field label="Device" htmlFor="ch-dev"><select id="ch-dev" value={device} onChange={(e) => { setDevice(e.target.value); setOffset(0) }}><option value="">All devices</option>{(devices.data?.devices || []).map((d: any) => <option key={d.id} value={d.id}>{d.display_name}</option>)}</select></Field>
        </div>
        <RangePicker value={range} onChange={(v) => { setRange(v); setOffset(0) }} allowAll />
      </div>
      {r.error && !r.data ? <ErrorNotice error={r.error} onRetry={() => r.reload()} what="load channels" /> : null}
      {!r.data && r.loading ? <Loading lines={6} /> : null}
      {r.data && r.data.items.length === 0 ? <div className="card"><Empty title="No skipped segments in this period" icon="channel" /></div> : null}
      {r.data && r.data.items.length ? (
        <div className="card responsive-table">
          <table className="data">
            <thead><tr><th>Channel</th><th className="num">Skips</th><th className="num">Videos</th><th className="num">Segment length</th><th>Last skip</th><th><span className="sr-only">Details</span></th></tr></thead>
            <tbody>
              {r.data.items.map((c: any) => (
                <Fragment key={c.channel_key}>
                  <tr>
                    <td className="wrap-anywhere"><ChannelName c={c} />{c.attributed < c.skips ? <div className="subtle">{c.skips - c.attributed} skip(s) without a matched segment</div> : null}</td>
                    <td className="num">{fmtNumber(c.skips)}</td><td className="num">{fmtNumber(c.videos)}</td><td className="num">{fmtDuration(c.segment_duration_seconds)}</td><td>{fmtRelative(c.last_at)}</td>
                    <td><button className="btn btn-sm btn-ghost" aria-expanded={open === c.channel_key} onClick={() => setOpen(open === c.channel_key ? null : c.channel_key)}>Videos</button></td>
                  </tr>
                  {open === c.channel_key ? <tr><td colSpan={6}><Videos c={c} q={rq} /></td></tr> : null}
                </Fragment>
              ))}
            </tbody>
          </table>
          <div className="list-cards">
            {r.data.items.map((c: any) => (
              <div className="list-card" key={c.channel_key}>
                <div className="lc-head"><span className="lc-title"><ChannelName c={c} /></span><strong>{fmtNumber(c.skips)}</strong></div>
                <dl><dt>Videos</dt><dd>{fmtNumber(c.videos)}</dd><dt>Segment length</dt><dd>{fmtDuration(c.segment_duration_seconds)}</dd><dt>Last skip</dt><dd>{fmtRelative(c.last_at)}</dd></dl>
                <button className="btn btn-sm btn-ghost" aria-expanded={open === c.channel_key} onClick={() => setOpen(open === c.channel_key ? null : c.channel_key)}>{open === c.channel_key ? 'Hide videos' : 'Show videos'}</button>
                {open === c.channel_key ? <Videos c={c} q={rq} /> : null}
              </div>
            ))}
          </div>
          <div className="row" style={{ justifyContent: 'space-between', marginTop: 12 }}>
            <button className="btn btn-sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</button>
            <span className="subtle">{offset + 1}–{Math.min(offset + 50, r.data.total)} of {r.data.total}</span>
            <button className="btn btn-sm" disabled={offset + 50 >= r.data.total} onClick={() => setOffset(offset + 50)}>Next</button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
