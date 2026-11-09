import { useEffect, useRef, useState } from 'react'
import { api, download } from '../api'
import { Icon } from '../components/Icon'
import { Badge, ErrorNotice, Field, Loading, Notice, Segmented } from '../components/ui'
import { fmtRelative, fmtTime } from '../format'
import { useResource } from '../hooks'
import { useToast } from '../toast'

function bytes(n: number | null | undefined) {
  if (n == null) return '—'
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let i = 0, v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`
}

function DebugControls({ onChange }: { onChange: () => void }) {
  const r = useResource<any>('/api/logs/levels')
  const toast = useToast()
  const [component, setComponent] = useState('')
  const [level, setLevel] = useState('DEBUG')
  const [minutes, setMinutes] = useState(30)
  const [, tick] = useState(0)
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 15000); return () => clearInterval(t) }, [])
  const lv = r.data?.levels
  const exp = lv?.global_override_expires_at
  const debugOn = lv?.global === 'DEBUG' && lv?.global_base !== 'DEBUG'
  async function call(p: Promise<any>, msg: string) { try { await p; toast(msg); r.reload(true); onChange() } catch (e: any) { toast(e.message, 'bad') } }
  return (
    <section className="card">
      <div className="card-head"><h2>Companion log level</h2>{debugOn ? <Badge kind="warn" icon="alert">Debug mode on</Badge> : <Badge>{lv?.global || '…'}</Badge>}</div>
      {debugOn ? <Notice kind="warn" title="Verbose logging is active">Ends {exp ? `${fmtRelative(exp)} (${fmtTime(exp, { date: false })})` : 'when turned off'}; the level then returns to {lv.global_base}. Debug logs never contain passwords, cookies, tokens or screen IDs.</Notice> : null}
      <div className="row">
        {debugOn
          ? <button className="btn" onClick={() => call(api('/api/logs/debug-mode', { method: 'DELETE' }), 'Debug mode turned off')}>Turn off debug mode</button>
          : <button className="btn btn-primary" onClick={() => call(api('/api/logs/debug-mode', { body: { minutes: r.data?.default_debug_minutes || 30 } }), 'Debug mode on')}>Enable debug mode for {r.data?.default_debug_minutes || 30} minutes</button>}
      </div>
      <details className="tech" style={{ marginTop: 12 }}>
        <summary>Per-component overrides</summary>
        <div className="filters" style={{ marginTop: 8 }}>
          <Field label="Component (blank = global)" htmlFor="lv-comp"><input id="lv-comp" value={component} onChange={(e) => setComponent(e.target.value.trim())} placeholder="e.g. ingest, auth, upstream.docker" /></Field>
          <Field label="Level" htmlFor="lv-lvl"><select id="lv-lvl" value={level} onChange={(e) => setLevel(e.target.value)}>{['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map((l) => <option key={l}>{l}</option>)}</select></Field>
          <Field label="For (minutes, blank = until restart)" htmlFor="lv-min"><input id="lv-min" type="number" min={1} value={minutes || ''} onChange={(e) => setMinutes(Number(e.target.value))} /></Field>
          <button className="btn" onClick={() => call(api('/api/logs/levels', { method: 'PUT', body: { component, level, minutes: minutes || null } }), 'Level set')}>Set</button>
        </div>
        {lv && Object.keys(lv.components || {}).length ? (
          <ul>{Object.entries(lv.components).map(([k, v]: any) => <li key={k} className="row"><code>{k}</code> = {v}{lv.component_override_expires?.[k] ? <span className="subtle"> until {fmtTime(lv.component_override_expires[k], { date: false })}</span> : null}
            <button className="btn btn-sm btn-ghost" onClick={() => call(api(`/api/logs/levels/${encodeURIComponent(k)}`, { method: 'DELETE' }), 'Override cleared')}>Clear</button></li>)}</ul>
        ) : <p className="subtle">No component overrides. Defaults come from LOG_LEVEL and LOG_LEVEL_&lt;COMPONENT&gt; environment variables.</p>}
      </details>
    </section>
  )
}

function LogViewer({ kind }: { kind: 'companion' | 'upstream' }) {
  const [level, setLevel] = useState(kind === 'companion' ? 'INFO' : '')
  const [component, setComponent] = useState('')
  const [search, setSearch] = useState('')
  const [paused, setPaused] = useState(false)
  const [records, setRecords] = useState<any[]>([])
  const [meta, setMeta] = useState<any>(null)
  const [error, setError] = useState<any>(null)
  const lastSeq = useRef(0)
  const lastId = useRef(0)
  const box = useRef<HTMLDivElement>(null)
  const params = () => {
    const p = new URLSearchParams()
    if (level) p.set('level', level)
    if (kind === 'companion' && component) p.set('component', component)
    if (search) p.set('search', search)
    return p
  }
  async function load(initial: boolean) {
    try {
      const p = params()
      if (kind === 'companion') {
        p.set('limit', '300')
        if (!initial && lastSeq.current) p.set('after_seq', String(lastSeq.current))
        const r = await api(`/api/logs/companion?${p}`)
        setMeta(r)
        if (initial) setRecords(r.records)
        else if (r.records.length) setRecords((x) => [...x, ...r.records].slice(-1000))
        lastSeq.current = r.latest_seq
      } else {
        p.set('limit', '300')
        if (!initial && lastId.current) p.set('after_id', String(lastId.current))
        const r = await api(`/api/logs/upstream?${p}`)
        setMeta(r)
        const recs = initial ? [...r.records].reverse() : r.records
        if (initial) setRecords(recs)
        else if (recs.length) setRecords((x) => [...x, ...recs].slice(-1000))
        if (recs.length) lastId.current = Math.max(lastId.current, ...recs.map((x: any) => x.id))
      }
      setError(null)
    } catch (e) { setError(e) }
  }
  useEffect(() => { lastSeq.current = 0; lastId.current = 0; load(true) }, [level, component, search, kind])
  useEffect(() => {
    if (paused) return
    const t = setInterval(() => { if (document.visibilityState === 'visible') load(false) }, 4000)
    return () => clearInterval(t)
  }, [paused, level, component, search, kind])
  useEffect(() => { if (!paused && box.current) box.current.scrollTop = box.current.scrollHeight }, [records, paused])
  return (
    <div className="stack">
      <div className="filters">
        <Field label="Minimum level" htmlFor={`${kind}-lvl`}><select id={`${kind}-lvl`} value={level} onChange={(e) => setLevel(e.target.value)}><option value="">All</option>{['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map((l) => <option key={l}>{l}</option>)}</select></Field>
        {kind === 'companion' ? <Field label="Component" htmlFor="comp"><select id="comp" value={component} onChange={(e) => setComponent(e.target.value)}><option value="">All</option>{(meta?.components || []).map((c: string) => <option key={c}>{c}</option>)}</select></Field> : null}
        <Field label="Search" htmlFor={`${kind}-q`}><input id={`${kind}-q`} type="search" value={search} onChange={(e) => setSearch(e.target.value)} /></Field>
        <button className="btn" onClick={() => setPaused((p) => !p)} aria-pressed={paused}><Icon name={paused ? 'play' : 'pause'} /> {paused ? 'Resume' : 'Pause'}</button>
        {kind === 'companion' ? <button className="btn" onClick={() => download(`/api/logs/companion/download?${params()}`, 'companion-logs.ndjson')}><Icon name="download" /> Download</button> : null}
      </div>
      {error ? <ErrorNotice error={error} what="load logs" /> : null}
      {kind === 'upstream' ? <p className="subtle">iSponsorBlockTV’s own output as collected by the companion. Screen IDs and YouTube tokens are replaced before storage. Kept separately from activity history and pruned automatically.</p> : null}
      <div className="log-box" ref={box} role="log" aria-live="off" aria-label={`${kind} log`} tabIndex={0}>
        {records.length === 0 ? <p className="muted" style={{ padding: 12 }}>No log records match.</p> : records.map((r, i) => kind === 'companion' ? (
          <div key={r.seq ?? i} className="log-line"><span className="subtle">{fmtTime(r.ts, { date: false, seconds: true })}</span> <span className={`lvl lvl-${r.level}`}>{r.level}</span><span className="subtle">{r.component}</span> {r.msg}{r.event ? <span className="subtle"> [{r.event}]</span> : null}{r.repeat ? <Badge kind="warn">+{r.repeat} similar</Badge> : null}
            {Object.keys(r).filter((k) => !['ts', 'level', 'component', 'event', 'msg', 'seq', 'exc', 'repeat'].includes(k)).length ? <span className="subtle"> {JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k]) => !['ts', 'level', 'component', 'event', 'msg', 'seq', 'exc', 'repeat'].includes(k))))}</span> : null}
            {r.exc ? <details><summary>Stack trace</summary><pre>{r.exc}</pre></details> : null}</div>
        ) : (
          <div key={r.id} className="log-line"><span className="subtle">{fmtTime(r.at, { date: true, seconds: true })}</span> {r.level ? <span className={`lvl lvl-${r.level}`}>{r.level}</span> : null}<span className="subtle">{r.parsed}</span> {r.message}</div>
        ))}
      </div>
      {kind === 'companion' && meta?.file ? <p className="subtle">{meta.file.enabled === false ? 'File logging is off (stdout only; Docker keeps those logs per its logging driver).' : `Log files: ${bytes(meta.file.directory_bytes)} of ${bytes(meta.file.budget_bytes)} budget, ${meta.file.archives} archive(s), kept ${meta.file.retention_days} days.`} Showing the most recent {meta.total_in_buffer} records held in memory.</p> : null}
    </div>
  )
}

export default function DiagnosticsPage() {
  const health = useResource<any>('/api/health')
  const caps = useResource<any>('/api/capabilities')
  const audit = useResource<any>('/api/audit?limit=50')
  const [tab, setTab] = useState<'companion' | 'upstream'>('companion')
  const [includeConfig, setIncludeConfig] = useState(false)
  const toast = useToast()
  const h = health.data
  const col = h?.collection
  return (
    <div>
      <div className="page-head"><div><h1>Diagnostics</h1><p>Health, logs, and a shareable diagnostic bundle.</p></div>
        <button className="btn" onClick={() => { health.reload(); caps.reload() }}><Icon name="refresh" /> Refresh</button></div>
      {health.error && !h ? <ErrorNotice error={health.error} onRetry={() => health.reload()} what="load health" /> : null}
      {!h ? <Loading /> : (
        <div className="stack" style={{ gap: 16 }}>
          <div className="tiles">
            <div className="tile"><div className="label">Companion</div><div className="value" style={{ fontSize: '1.1rem' }}>{h.database.healthy ? 'Running' : 'Degraded'}</div><div className="note">v{h.version} · up {fmtRelative(h.started_at).replace(' ago', '')}</div></div>
            <div className="tile"><div className="label">Collection</div><div className="value" style={{ fontSize: '1.1rem' }}>{col.status}</div><div className="note">{col.source_kind} · {col.lines_processed} lines · {col.duplicates_skipped} duplicates skipped</div></div>
            <div className="tile"><div className="label">iSponsorBlockTV container</div><div className="value" style={{ fontSize: '1.1rem' }}>{col.container?.state || 'unknown'}</div><div className="note">{col.container ? `${col.container.image} · restarts ${col.container.restart_count}` : 'Docker API not available'}</div></div>
            <div className="tile"><div className="label">Database</div><div className="value" style={{ fontSize: '1.1rem' }}>{bytes(h.database.size_bytes)}</div><div className="note">{h.disk ? `${bytes(h.disk.free)} free on data volume` : ''}</div></div>
          </div>
          {col.status !== 'connected' ? <Notice kind="warn" title="Collection is not running" details={col}>{col.detail}</Notice> : null}
          {h.logging.degraded ? <Notice kind="warn" title="File logging is degraded">{h.logging.file.degraded}. Logs continue on stdout.</Notice> : null}
          {col.upstream_debug ? <Notice kind="info">iSponsorBlockTV runs with <code>--debug</code>. The companion does not need it; it makes upstream logs much larger.</Notice> : null}
          <DebugControls onChange={() => health.reload(true)} />
          <section className="card">
            <div className="card-head"><h2>Logs</h2><Segmented label="Log source" value={tab} onChange={setTab} options={[{ value: 'companion', label: 'Companion' }, { value: 'upstream', label: 'iSponsorBlockTV' }]} /></div>
            <LogViewer kind={tab} key={tab} />
          </section>
          <section className="card">
            <h2>Diagnostic bundle</h2>
            <p className="muted">A zip with versions, health, integration capabilities, collection gaps, recent apply attempts, the audit trail, and recent logs. Passwords, cookies, tokens, screen IDs and the database are never included. Device names and video IDs are included; review before sharing publicly.</p>
            <label className="check"><input type="checkbox" checked={includeConfig} onChange={(e) => setIncludeConfig(e.target.checked)} /><span>Include configuration with secrets removed</span></label>
            <button className="btn btn-primary" onClick={() => download(`/api/diagnostics/bundle?include_config=${includeConfig}`, 'diagnostics.zip').catch((e) => toast(e.message, 'bad'))}><Icon name="download" /> Download bundle</button>
          </section>
          <section className="card">
            <h2>Integration capabilities</h2>
            {caps.data ? <>
              <p className="subtle">Parser {caps.data.parser_version}; supports iSponsorBlockTV {caps.data.supported_upstream}. Detected version: {caps.data.detected_upstream_version || 'not labelled on the image'}.</p>
              <div className="table-wrap"><table className="data"><thead><tr><th>Capability</th><th>Kind</th><th>Now</th><th>Source</th></tr></thead><tbody>
                {caps.data.capabilities.map((c: any) => <tr key={c.name}><td>{c.name}</td><td>{c.status}</td><td>{c.available ? <Badge kind="good" icon="check">Yes</Badge> : <Badge icon="x">No</Badge>}</td><td className="subtle">{c.source}</td></tr>)}
              </tbody></table></div>
            </> : <Loading />}
          </section>
          <details className="card">
            <summary style={{ minHeight: 44, display: 'flex', alignItems: 'center', cursor: 'pointer', fontWeight: 650 }}>Recent administrative changes</summary>
            {audit.data?.items?.length ? <ul className="feed">{audit.data.items.map((a: any) => <li key={a.id} style={{ padding: '6px 0' }}><strong>{a.action}</strong> → {a.result} <span className="subtle">by {a.actor_username || 'system'} · {fmtTime(a.at)}{a.fields ? ` · ${a.fields}` : ''}</span></li>)}</ul> : <p className="muted">None.</p>}
          </details>
          <details className="card">
            <summary style={{ minHeight: 44, display: 'flex', alignItems: 'center', cursor: 'pointer', fontWeight: 650 }}>Raw health report</summary>
            <pre className="mono" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: '.78rem' }}>{JSON.stringify(h, null, 2)}</pre>
          </details>
        </div>
      )}
    </div>
  )
}
