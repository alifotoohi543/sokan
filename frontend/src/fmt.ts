const fa = new Intl.NumberFormat('fa-IR')
const fa1 = new Intl.NumberFormat('fa-IR', { maximumFractionDigits: 1 })
const fa2 = new Intl.NumberFormat('fa-IR', { maximumFractionDigits: 2 })

export const n = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? '—' : fa.format(Math.round(v)))
export const n1 = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? '—' : fa1.format(v))
export const n2 = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? '—' : fa2.format(v))
export const toman = (v: number | null | undefined) => (v == null ? '—' : `${n(v)} تومان`)
export const pct = (v: number | null | undefined) => (v == null ? '—' : `${n(v)}٪`)

export function ago(ts: number | null | undefined): string {
  if (!ts) return '—'
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return `${n(s)} ثانیه پیش`
  if (s < 3600) return `${n(s / 60)} دقیقه پیش`
  if (s < 86400) return `${n(s / 3600)} ساعت پیش`
  return `${n(s / 86400)} روز پیش`
}

export function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('fa-IR', { hour: '2-digit', minute: '2-digit' })
}

export const PLATFORM_FA: Record<string, string> = {
  docker: 'Docker', node: 'Node.js', next: 'Next.js', react: 'React', vue: 'Vue', angular: 'Angular', static: 'استاتیک',
  django: 'Django', flask: 'Flask', python: 'Python', laravel: 'Laravel', php: 'PHP', go: 'Go', dotnet: '.NET',
}
