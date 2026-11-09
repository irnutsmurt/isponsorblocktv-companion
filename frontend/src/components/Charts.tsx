import { useId, useRef, useState } from 'react'
import { catLabel, fmtDuration, fmtNumber } from '../format'

type Bucket = { bucket: string; [k: string]: any }

/** Single-series column chart with per-bar hover/focus tooltip and a table fallback. */
export function ColumnChart({ data, valueKey, label, formatValue = fmtNumber, formatBucket }: { data: Bucket[]; valueKey: string; label: string; formatValue?: (n: number) => string; formatBucket: (b: string) => string }) {
  const [hover, setHover] = useState<number | null>(null)
  const [showTable, setShowTable] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const id = useId()
  const W = 640, H = 200, padL = 36, padB = 24, padT = 8
  const max = Math.max(1, ...data.map((d) => d[valueKey] || 0))
  const nice = niceMax(max)
  const n = data.length || 1
  const bw = (W - padL) / n
  const barW = Math.max(2, Math.min(28, bw - 2))
  const ticks = [0, nice / 2, nice]
  const labelEvery = Math.ceil(n / 8)
  const total = data.reduce((s, d) => s + (d[valueKey] || 0), 0)
  return (
    <div className="chart" ref={ref}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${id}-t`}>
        <title id={`${id}-t`}>{`${label}: ${formatValue(total)} total over ${n} periods`}</title>
        {ticks.map((t) => {
          const y = padT + (H - padB - padT) * (1 - t / nice)
          return <g key={t}><line className="grid-line" x1={padL} x2={W} y1={y} y2={y} /><text className="axis-label" x={padL - 6} y={y + 4} textAnchor="end">{formatValue(t)}</text></g>
        })}
        {data.map((d, i) => {
          const v = d[valueKey] || 0
          const h = (H - padB - padT) * (v / nice)
          const x = padL + i * bw + (bw - barW) / 2
          const y = H - padB - h
          const r = Math.min(4, barW / 2, h)
          return (
            <g key={d.bucket}>
              {v > 0 ? <path className={`bar ${hover === i ? 'hover' : ''}`} d={`M${x},${H - padB} V${y + r} Q${x},${y} ${x + r},${y} H${x + barW - r} Q${x + barW},${y} ${x + barW},${y + r} V${H - padB} Z`} /> : null}
              <rect className="hit" x={padL + i * bw} y={padT} width={bw} height={H - padB - padT} tabIndex={-1}
                onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onTouchStart={() => setHover(i)} />
              {i % labelEvery === 0 ? <text className="axis-label" x={padL + i * bw + bw / 2} y={H - 6} textAnchor="middle">{formatBucket(d.bucket)}</text> : null}
            </g>
          )
        })}
      </svg>
      {hover != null && data[hover] ? (
        <div className="chart-tooltip" style={{ left: `clamp(0px, calc(${((padL + hover * bw + bw / 2) / W) * 100}% - 60px), calc(100% - 140px))`, top: 0 }}>
          <div className="tt-title">{formatBucket(data[hover].bucket)}</div>
          <div>{label}: <strong>{formatValue(data[hover][valueKey] || 0)}</strong></div>
        </div>
      ) : null}
      <button className="btn btn-ghost btn-sm" onClick={() => setShowTable((s) => !s)} aria-expanded={showTable}>{showTable ? 'Hide table' : 'Show as table'}</button>
      {showTable ? (
        <div className="table-wrap"><table className="data"><thead><tr><th>Period</th><th className="num">{label}</th></tr></thead>
          <tbody>{data.map((d) => <tr key={d.bucket}><td>{formatBucket(d.bucket)}</td><td className="num">{formatValue(d[valueKey] || 0)}</td></tr>)}</tbody></table></div>
      ) : null}
    </div>
  )
}

function niceMax(v: number) {
  if (v <= 4) return 4
  const p = Math.pow(10, Math.floor(Math.log10(v)))
  const m = v / p
  const step = m <= 2 ? 2 : m <= 5 ? 5 : 10
  return step * p
}

/** Horizontal magnitude bars with direct labels (no colour-only encoding). */
export function CategoryBars({ items }: { items: { category: string; count: number; segment_duration_seconds: number }[] }) {
  const max = Math.max(1, ...items.map((i) => i.count))
  return (
    <ul className="hbar-list" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
      {items.map((i) => (
        <li key={i.category} className="hbar-row">
          <span className="hbar-label">{catLabel(i.category)}</span>
          <span className="hbar-value">{fmtNumber(i.count)} skips{i.segment_duration_seconds > 0 ? ` · ${fmtDuration(i.segment_duration_seconds)}` : ''}</span>
          <div className="hbar-track" aria-hidden><div className="hbar-fill" style={{ width: `${(i.count / max) * 100}%` }} /></div>
        </li>
      ))}
    </ul>
  )
}
