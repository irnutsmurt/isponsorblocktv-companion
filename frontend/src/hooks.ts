import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'

/** Load data with loading/error/stale state. Keeps last good data on error (never zeroes). */
export function useResource<T>(path: string | null, deps: any[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState<boolean>(!!path)
  const [loadedAt, setLoadedAt] = useState<number | null>(null)
  const ctrl = useRef<AbortController | null>(null)
  const reload = useCallback(async (quiet = false) => {
    if (!path) return
    ctrl.current?.abort()
    const c = new AbortController()
    ctrl.current = c
    if (!quiet) setLoading(true)
    try {
      const d = await api<T>(path, { signal: c.signal })
      setData(d)
      setError(null)
      setLoadedAt(Date.now())
    } catch (e: any) {
      if (e?.name !== 'AbortError') setError(e)
    } finally {
      if (ctrl.current === c) setLoading(false)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps])
  useEffect(() => { reload(); return () => ctrl.current?.abort() }, [reload])
  return { data, error, loading, reload, loadedAt, setData }
}

/** Persist safe (non-secret) form drafts in sessionStorage so rotation/reload/network loss keeps them. */
export function useDraft<T>(key: string, initial: T): [T, (v: T | ((p: T) => T)) => void, () => void] {
  const storageKey = `isbtv-draft:${key}`
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = sessionStorage.getItem(storageKey)
      if (raw) return JSON.parse(raw) as T
    } catch { /* storage unavailable */ }
    return initial
  })
  useEffect(() => {
    try { sessionStorage.setItem(storageKey, JSON.stringify(value)) } catch { /* ignore */ }
  }, [storageKey, value])
  const clear = useCallback(() => { try { sessionStorage.removeItem(storageKey) } catch { /* ignore */ } }, [storageKey])
  return [value, setValue, clear]
}

export function useOnline() {
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine)
  useEffect(() => {
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off) }
  }, [])
  return online
}

export function useMediaQuery(q: string) {
  const [m, setM] = useState(() => typeof window !== 'undefined' && window.matchMedia(q).matches)
  useEffect(() => {
    const mq = window.matchMedia(q)
    const h = () => setM(mq.matches)
    mq.addEventListener('change', h)
    return () => mq.removeEventListener('change', h)
  }, [q])
  return m
}

/** Hides the bottom nav while an input is focused on small screens so the keyboard never covers the submit control. */
export function useKeyboardAwareness() {
  useEffect(() => {
    const isField = (el: Element | null) => !!el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && !(el as HTMLInputElement).type?.match(/checkbox|radio|button|submit/)
    const onFocus = (e: FocusEvent) => {
      if (isField(e.target as Element)) {
        document.body.classList.add('keyboard-open')
        setTimeout(() => (e.target as HTMLElement).scrollIntoView?.({ block: 'center', behavior: 'smooth' }), 300)
      }
    }
    const onBlur = () => setTimeout(() => { if (!isField(document.activeElement)) document.body.classList.remove('keyboard-open') }, 50)
    document.addEventListener('focusin', onFocus)
    document.addEventListener('focusout', onBlur)
    return () => { document.removeEventListener('focusin', onFocus); document.removeEventListener('focusout', onBlur) }
  }, [])
}
