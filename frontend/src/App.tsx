import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api'
import { AuthProvider, useAuth } from './auth'
import { Icon } from './components/Icon'
import { ReauthHost } from './components/Reauth'
import { Dialog, Notice } from './components/ui'
import { setDisplayTimezone } from './format'
import { useKeyboardAwareness, useOnline } from './hooks'
import { onLiveStatus } from './live'
import { ToastProvider } from './toast'
import { LoginPage, SetupPage } from './pages/AuthPages'
import DashboardPage from './pages/Dashboard'
import DevicesPage, { DeviceDetailPage } from './pages/Devices'
import ActivityPage from './pages/Activity'
import ChannelsPage from './pages/Channels'
import ConfigPage from './pages/Config'
import DiagnosticsPage from './pages/Diagnostics'
import AccountPage from './pages/Account'
import GettingStarted from './pages/GettingStarted'

const NAV = [
  { to: '/', label: 'Overview', icon: 'home', end: true },
  { to: '/devices', label: 'Devices', icon: 'tv' },
  { to: '/activity', label: 'Activity', icon: 'list' },
  { to: '/channels', label: 'Channels', icon: 'channel' },
  { to: '/config', label: 'Configuration', icon: 'settings' },
  { to: '/diagnostics', label: 'Diagnostics', icon: 'wrench' },
  { to: '/account', label: 'Account & security', icon: 'user' },
]

export function ThemeToggle() {
  const [theme, setTheme] = useState<string>(() => { try { return localStorage.getItem('isbtv-theme') || 'system' } catch { return 'system' } })
  useEffect(() => {
    if (theme === 'system') document.documentElement.removeAttribute('data-theme')
    else document.documentElement.setAttribute('data-theme', theme)
    try { theme === 'system' ? localStorage.removeItem('isbtv-theme') : localStorage.setItem('isbtv-theme', theme) } catch { /* ignore */ }
  }, [theme])
  const next = theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system'
  return (
    <button className="icon-btn" onClick={() => setTheme(next)} aria-label={`Theme: ${theme}. Switch to ${next}.`} title={`Theme: ${theme}`}>
      <Icon name={theme === 'light' ? 'sun' : theme === 'dark' ? 'moon' : 'monitor'} />
    </button>
  )
}

function Shell() {
  const { me, logout } = useAuth()
  const online = useOnline()
  const [live, setLive] = useState('closed')
  const [more, setMore] = useState(false)
  const loc = useLocation()
  useKeyboardAwareness()
  useEffect(() => onLiveStatus(setLive), [])
  useEffect(() => { setMore(false) }, [loc.pathname])
  useEffect(() => { api('/api/settings').then((s) => setDisplayTimezone(s.timezone)).catch(() => {}) }, [])
  const mobileMain = NAV.slice(0, 4)
  return (
    <div className="shell">
      <a href="#main" className="skip-link">Skip to content</a>
      <nav className="sidebar" aria-label="Main">
        <div className="brand"><img src="/favicon.svg" alt="" /> iSBTV Web UI</div>
        {NAV.map((n) => <NavLink key={n.to} to={n.to} end={n.end} className="nav-link"><Icon name={n.icon} />{n.label}</NavLink>)}
        <div className="sidebar-foot">
          <div className="row subtle" style={{ padding: '0 8px' }}><span>Signed in as <strong>{me?.username}</strong></span></div>
          <div className="row"><ThemeToggle /><button className="btn btn-ghost btn-sm" onClick={logout}><Icon name="logout" /> Sign out</button></div>
        </div>
      </nav>
      <header className="topbar">
        <div className="brand" style={{ padding: 0 }}><img src="/favicon.svg" alt="" /> Web UI</div>
        <div className="row"><ThemeToggle /><button className="icon-btn" onClick={logout} aria-label="Sign out"><Icon name="logout" /></button></div>
      </header>
      <main id="main" tabIndex={-1}>
        {!online ? <Notice kind="warn" title="You are offline">Showing the last loaded data. Changes are not sent until you reconnect, and nothing is replayed automatically.</Notice> : null}
        {online && live === 'closed' && document.visibilityState === 'visible' ? <span className="sr-only">Live updates reconnecting</span> : null}
        {me?.insecure_transport && location.hostname !== 'localhost' ? <Notice kind="warn" title="Not using HTTPS">Passwords and session cookies are sent unencrypted. See Account &amp; security → Access for HTTPS setup.</Notice> : null}
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/getting-started" element={<GettingStarted />} />
          <Route path="/devices" element={<DevicesPage />} />
          <Route path="/devices/:id" element={<DeviceDetailPage />} />
          <Route path="/activity" element={<ActivityPage />} />
          <Route path="/channels" element={<ChannelsPage />} />
          <Route path="/config" element={<ConfigPage />} />
          <Route path="/diagnostics" element={<DiagnosticsPage />} />
          <Route path="/account" element={<AccountPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <nav className="bottom-nav" aria-label="Main">
        {mobileMain.map((n) => <NavLink key={n.to} to={n.to} end={n.end}><Icon name={n.icon} />{n.label}</NavLink>)}
        <button onClick={() => setMore(true)} aria-haspopup="dialog"><Icon name="more" />More</button>
      </nav>
      <Dialog open={more} onClose={() => setMore(false)} title="More">
        <div className="stack">
          {NAV.slice(4).map((n) => <NavLink key={n.to} to={n.to} className="nav-link"><Icon name={n.icon} />{n.label}</NavLink>)}
          <NavLink to="/getting-started" className="nav-link"><Icon name="help" />Getting started</NavLink>
        </div>
      </Dialog>
      <ReauthHost />
    </div>
  )
}

function Gate() {
  const { status, error, refresh } = useAuth()
  if (status === 'loading') return <div className="auth-wrap"><div role="status">Loading…</div></div>
  if (status === 'error') return (
    <div className="auth-wrap"><div className="card auth-card">
      <Notice kind="bad" title="The webui is not responding normally" actions={<button className="btn btn-sm" onClick={refresh}>Retry</button>}>
        <p>{error || 'Unknown error.'} If this persists, check the webui container’s logs on your NAS.</p>
      </Notice>
    </div></div>
  )
  if (status === 'setup') return <SetupPage />
  if (status === 'anon') return <LoginPage />
  return <Shell />
}

export default function App() {
  return <ToastProvider><AuthProvider><Gate /></AuthProvider></ToastProvider>
}
