import { useState } from 'react'
import { Link } from 'react-router-dom'
import { PairWizard } from '../components/PairWizard'
import { Badge, ErrorNotice, Loading, Notice } from '../components/ui'
import { useResource } from '../hooks'

export default function GettingStarted() {
  const cfg = useResource<any>('/api/config')
  const health = useResource<any>('/api/health')
  const [done, setDone] = useState(false)
  if (cfg.error) return <ErrorNotice error={cfg.error} onRetry={() => cfg.reload()} what="check the setup" />
  if (!cfg.data || !health.data) return <Loading lines={6} />
  const c = cfg.data
  const h = health.data
  const col = h.collection
  const checks = [
    { ok: c.directory?.exists, label: 'iSponsorBlockTV data folder is mounted', fix: `Mount the iSponsorBlockTV data folder into the webui at ${c.directory?.path}.` },
    { ok: c.writable, label: 'The web UI can write config.json', fix: c.write_problem },
    { ok: !c.parse_error, label: c.exists ? 'Existing configuration is readable' : 'No configuration yet (one will be created)', fix: c.parse_error },
    { ok: col.status === 'connected', warn: true, label: 'The web UI can read iSponsorBlockTV’s activity', fix: col.detail },
    { ok: c.apply_capability?.mode === 'restart', warn: true, label: 'Settings can be applied from this page', fix: c.apply_capability?.reason },
  ]
  const blocking = checks.some((x) => !x.ok && !x.warn)
  const existing = (c.devices || []).length
  return (
    <div>
      <div className="page-head"><div><h1>Getting started</h1><p>Connect iSponsorBlockTV to your TVs without a terminal.</p></div></div>
      <section className="card">
        <h2>1. Check the deployment</h2>
        <ul className="feed">{checks.map((x) => (
          <li key={x.label} style={{ padding: '8px 0' }}>
            {x.ok ? <Badge kind="good" icon="check">OK</Badge> : x.warn ? <Badge kind="warn" icon="alert">Limited</Badge> : <Badge kind="bad" icon="x">Problem</Badge>} {x.label}
            {!x.ok && x.fix ? <p className="subtle" style={{ margin: '4px 0 0' }}>{x.fix}</p> : null}
          </li>))}</ul>
        {checks[4].ok === false ? <p className="subtle">Without Docker access you can still pair TVs here; you will be asked to restart iSponsorBlockTV from your NAS once, and this app confirms when it picked up the change.</p> : null}
      </section>
      <section className="card">
        <h2>2. {existing ? 'Your TVs' : 'Pair your first TV'}</h2>
        {existing && !done ? (
          <>
            <Notice kind="good" title={`Found ${existing} configured TV(s)`}>Your existing configuration was loaded; nothing needs to be re-paired. <Link to="/devices">View devices</Link></Notice>
          </>
        ) : null}
        {blocking ? <Notice kind="bad">Fix the problems above first, then reload this page.</Notice> : done ? (
          <Notice kind="good" title="All set">Your TV is configured. <Link to="/">Go to the overview</Link> or <Link to="/devices">add another TV</Link>.</Notice>
        ) : (
          <PairWizard etag={c.exists ? c.etag : null} applyCapability={c.apply_capability} onDone={() => { setDone(true); cfg.reload(true) }} />
        )}
      </section>
    </div>
  )
}
