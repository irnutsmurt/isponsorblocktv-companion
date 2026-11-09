export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || isNaN(seconds)) return '—'
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60 ? `${s % 60}s` : ''}`.trim()
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

export function fmtNumber(n: number | null | undefined): string {
  if (n == null) return '—'
  return new Intl.NumberFormat().format(n)
}

let tzName = 'UTC'
export function setDisplayTimezone(tz: string) { tzName = tz || 'UTC' }
export function displayTimezone() { return tzName }

export function fmtTime(iso: string | null | undefined, opts: { date?: boolean; seconds?: boolean } = { date: true }): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  try {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: tzName,
      ...(opts.date !== false ? { year: 'numeric', month: 'short', day: 'numeric' } : {}),
      hour: '2-digit', minute: '2-digit', ...(opts.seconds ? { second: '2-digit' } : {}),
    }).format(d)
  } catch {
    return d.toLocaleString()
  }
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const d = new Date(iso).getTime()
  if (isNaN(d)) return iso
  const diff = (Date.now() - d) / 1000
  const abs = Math.abs(diff)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  if (abs < 45) return 'just now'
  if (abs < 3600) return rtf.format(-Math.round(diff / 60), 'minute')
  if (abs < 86400) return rtf.format(-Math.round(diff / 3600), 'hour')
  return rtf.format(-Math.round(diff / 86400), 'day')
}

export const CATEGORY_LABELS: Record<string, string> = {
  sponsor: 'Sponsor', selfpromo: 'Self promotion', intro: 'Intro', outro: 'Outro', music_offtopic: 'Music: off-topic',
  interaction: 'Interaction reminder', exclusive_access: 'Exclusive access', preview: 'Preview / recap', filler: 'Filler',
  hook: 'Hook / greetings', unknown: 'Category unknown', pending: 'Not matched yet',
}
export const catLabel = (c: string) => CATEGORY_LABELS[c] || c

export const KIND_LABELS: Record<string, string> = {
  skip: 'Segment skip requested', ad_skip: 'Ad skip requested', ad_mute: 'Ad muted', ad_unmute: 'Ad unmuted',
  connected: 'Connected to TV', disconnected: 'TV session ended', loaded: 'Loaded by iSponsorBlockTV', error: 'Error', warning: 'Warning',
}
