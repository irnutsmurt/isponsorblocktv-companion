import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
type Toast = { id: number; text: string; kind?: 'bad' }
const Ctx = createContext<(text: string, kind?: 'bad') => void>(() => {})
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([])
  const push = useCallback((text: string, kind?: 'bad') => {
    const id = Date.now() + Math.random()
    setItems((x) => [...x.slice(-2), { id, text, kind }])
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), kind === 'bad' ? 8000 : 4000)
  }, [])
  return (
    <Ctx.Provider value={push}>
      {children}
      <div className="toast-region" aria-live="polite" role="status">
        {items.map((t) => <div key={t.id} className={`toast ${t.kind || ''}`}>{t.text}</div>)}
      </div>
    </Ctx.Provider>
  )
}
export const useToast = () => useContext(Ctx)
