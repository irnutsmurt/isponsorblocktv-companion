import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../api'
import { Icon } from './Icon'

export function Notice({ kind = 'info', title, children, actions, details }: { kind?: 'info' | 'good' | 'warn' | 'bad'; title?: ReactNode; children?: ReactNode; actions?: ReactNode; details?: any }) {
  const icon = kind === 'good' ? 'check' : kind === 'info' ? 'info' : 'alert'
  return (
    <div className={`notice ${kind}`} role={kind === 'bad' ? 'alert' : 'status'}>
      <Icon name={icon} />
      <div className="body">
        {title ? <div className="title">{title}</div> : null}
        {children}
        {actions ? <div className="row" style={{ marginTop: 8 }}>{actions}</div> : null}
        {details ? (
          <details className="tech">
            <summary>Technical details</summary>
            <pre>{typeof details === 'string' ? details : JSON.stringify(details, null, 2)}</pre>
          </details>
        ) : null}
      </div>
    </div>
  )
}

export function ErrorNotice({ error, onRetry, what }: { error: unknown; onRetry?: () => void; what?: string }) {
  if (!error) return null
  const e = error as ApiError
  const msg = e?.message || String(error)
  let next = 'Try again in a moment.'
  if (e?.status === 0) next = 'Check that this device can reach the companion, then retry.'
  else if (e?.status === 503) next = 'The companion cannot use its storage. Open Diagnostics or check the container logs.'
  else if (e?.status === 409) next = 'Reload the latest state before trying again.'
  return (
    <Notice kind="bad" title={what ? `Could not ${what}` : 'Something went wrong'} details={e?.details}
      actions={onRetry ? <button className="btn btn-sm" onClick={onRetry}><Icon name="refresh" /> Retry</button> : undefined}>
      <p>{msg} {next}</p>
    </Notice>
  )
}

export function PasswordInput({ id, value, onChange, autoComplete, invalid, describedBy, autoFocus, name }: { id: string; value: string; onChange: (v: string) => void; autoComplete: string; invalid?: boolean; describedBy?: string; autoFocus?: boolean; name?: string }) {
  const [show, setShow] = useState(false)
  return (
    <div className="input-wrap">
      <input id={id} name={name || id} type={show ? 'text' : 'password'} value={value} onChange={(e) => onChange(e.target.value)} autoComplete={autoComplete}
        autoCapitalize="none" autoCorrect="off" spellCheck={false} aria-invalid={invalid || undefined} aria-describedby={describedBy} autoFocus={autoFocus} />
      <button type="button" className="icon-btn" onClick={() => setShow((s) => !s)} aria-label={show ? 'Hide password' : 'Show password'} aria-pressed={show}>
        <Icon name={show ? 'eyeOff' : 'eye'} />
      </button>
    </div>
  )
}

export function Dialog({ open, onClose, title, children, footer, dismissable = true }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; dismissable?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const d = ref.current
    if (!d) return
    if (open && !d.open) d.showModal()
    if (!open && d.open) d.close()
  }, [open])
  return (
    <dialog ref={ref} className="modal" aria-labelledby={titleId}
      onCancel={(e) => { e.preventDefault(); if (dismissable) onClose() }}>
      {open ? (
        <div className="modal-inner">
          <div className="modal-head">
            <h2 id={titleId}>{title}</h2>
            {dismissable ? <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="x" /></button> : null}
          </div>
          <div className="modal-body">{children}</div>
          {footer ? <div className="modal-foot">{footer}</div> : null}
        </div>
      ) : null}
    </dialog>
  )
}

export function ConfirmDialog({ open, title, children, confirmLabel, danger, busy, onConfirm, onCancel }: { open: boolean; title: string; children: ReactNode; confirmLabel: string; danger?: boolean; busy?: boolean; onConfirm: () => void; onCancel: () => void }) {
  return (
    <Dialog open={open} onClose={onCancel} title={title} footer={<>
      <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
      <button className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={onConfirm} disabled={busy}>{busy ? <span className="spinner" aria-hidden /> : null}{confirmLabel}</button>
    </>}>
      {children}
    </Dialog>
  )
}

export function Badge({ kind, icon, children }: { kind?: 'good' | 'warn' | 'bad' | 'info'; icon?: string; children: ReactNode }) {
  return <span className={`badge ${kind || ''}`}>{icon ? <Icon name={icon} /> : null}{children}</span>
}

export function Empty({ icon = 'info', title, children }: { icon?: string; title: string; children?: ReactNode }) {
  return <div className="empty"><Icon name={icon} /><h3>{title}</h3>{children}</div>
}

export function Loading({ label = 'Loading…', lines = 3 }: { label?: string; lines?: number }) {
  return (
    <div role="status" aria-live="polite" className="stack">
      <span className="sr-only">{label}</span>
      {Array.from({ length: lines }).map((_, i) => <div key={i} className="skeleton" style={{ height: 18, width: `${90 - i * 15}%` }} />)}
    </div>
  )
}

export function Field({ label, help, error, children, htmlFor }: { label: ReactNode; help?: ReactNode; error?: ReactNode; children: ReactNode; htmlFor: string }) {
  return (
    <div className="field">
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {help ? <div className="help" id={`${htmlFor}-help`}>{help}</div> : null}
      {error ? <div className="error" id={`${htmlFor}-error`} role="alert">{error}</div> : null}
    </div>
  )
}

export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((o) => <button key={o.value} type="button" aria-pressed={value === o.value} onClick={() => onChange(o.value)}>{o.label}</button>)}
    </div>
  )
}
