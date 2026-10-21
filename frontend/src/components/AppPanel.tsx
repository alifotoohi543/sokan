import { useEffect, useState } from 'react'
import { ExternalLink, Loader2, Stethoscope, Zap } from 'lucide-react'
import { getJSON, postJSON } from '../api'
import { UsageChart } from './Chart'
import { Stat } from './Cards'
import { PLATFORM_FA, n, n1 } from '../fmt'

export function AppPanel({ app, catalog, onAsk }: { app: string; catalog: Record<string, string>; onAsk: (t: string) => void }) {
  const [st, setSt] = useState<any>(null)
  const [m, setM] = useState<any>(null)
  const [h, setH] = useState<any>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let live = true
    getJSON(`/api/apps/${app}/status`).then((d) => live && setSt(d)).catch((e) => live && setErr(e.message))
    getJSON(`/api/apps/${app}/metrics?minutes=60`).then((d) => live && setM(d)).catch(() => {})
    getJSON(`/api/apps/${app}/health`).then((d) => live && setH(d)).catch(() => {})
    return () => { live = false }
  }, [app])

  async function toggle(kind: string, enabled: boolean) {
    const verb = enabled ? 'فعال' : 'غیرفعال'
    if (!window.confirm(`سیاست خودکار «${catalog[kind]}» برای ${app} ${verb} شود؟\nبا فعال‌سازی، این اقدام بدون پرسش مجدد انجام می‌شود (و در گزارش عملیات ثبت می‌شود).`)) return
    const r = await postJSON(`/api/apps/${app}/policies`, { kind, enabled })
    setH((x: any) => ({ ...x, policies: r.policies }))
  }

  const http = st?.http || {}
  const uptime = h?.history?.length ? h.history.filter((x: any) => x.ok).length / h.history.length : null
  return (
    <div className="space-y-3 p-3">
      <div className="flex items-center gap-2">
        <h2 className="ltr truncate text-base font-semibold">{app}</h2>
        {st?.url && <a href={st.url} target="_blank" rel="noreferrer" className="text-ink-3 hover:text-accent" aria-label="باز کردن سایت"><ExternalLink size={15} /></a>}
        <button onClick={() => onAsk(`یک بررسی کامل سلامت روی ${app} انجام بده`)}
          className="ms-auto inline-flex items-center gap-1 rounded-lg border border-accent/50 px-2 py-1 text-xs text-accent hover:bg-accent-soft">
          <Stethoscope size={13} />عیب‌یابی
        </button>
      </div>
      {err && <p className="text-xs text-crit">{err}</p>}
      {!st && !err ? <Loader2 size={18} className="animate-spin text-ink-3" /> : st && (
        <div className="grid grid-cols-2 gap-2">
          <Stat label="پاسخ سایت" value={http.status ? `HTTP ${http.status}` : 'بدون پاسخ'} sub={http.latency_s != null ? `${n1(http.latency_s)} ثانیه` : ''}
            tone={!http.status || http.status >= 500 ? 'crit' : http.latency_s > 2.5 ? 'warn' : 'ok'} />
          <Stat label="دسترس‌پذیری (۱ ساعت)" value={uptime == null ? '—' : `${n(uptime * 100)}٪`} sub={`${n(h?.history?.length)} بررسی`}
            tone={uptime == null ? 'default' : uptime > 0.98 ? 'ok' : uptime > 0.9 ? 'warn' : 'crit'} />
          <Stat label="پلن" value={<span className="ltr">{st.plan}</span>} sub={st.limits ? `${n1(st.limits.ram_gb)}GB · ${n1(st.limits.cpu)} CPU` : ''} />
          <Stat label="پلتفرم" value={PLATFORM_FA[st.platform] || st.platform} sub={st.scale === 0 ? 'خاموش' : st.status} tone={st.scale === 0 ? 'warn' : 'default'} />
        </div>
      )}
      <UsageChart title="RAM — ۶۰ دقیقه‌ی اخیر" unit="MB" points={m?.series?.mem_mb || []} limit={m?.limits?.ram_gb ? m.limits.ram_gb * 1024 : null} />
      <UsageChart title="CPU — ۶۰ دقیقه‌ی اخیر" unit="هسته" points={m?.series?.cpu || []} limit={m?.limits?.cpu} />
      <UsageChart title="زمان پاسخ سایت (پایش سکان)" unit="ثانیه"
        points={(h?.history || []).filter((x: any) => x.latency != null).map((x: any) => [x.ts, x.latency])} limit={2.5} height={90} />

      <section className="rounded-xl border border-line bg-panel-2 p-3">
        <h3 className="flex items-center gap-1.5 text-xs font-semibold"><Zap size={13} className="text-ai" />خودکارسازی (تأیید دائمی)</h3>
        <ul className="mt-2 space-y-2">
          {Object.entries(catalog).map(([k, label]) => {
            const on = !!h?.policies?.[k]
            return (
              <li key={k} className="flex items-start gap-2 text-xs">
                <button role="switch" aria-checked={on} onClick={() => toggle(k, !on)} aria-label={label}
                  className={`relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition ${on ? 'bg-accent' : 'bg-line-2'}`}>
                  <span className={`absolute top-0.5 size-4 rounded-full bg-white transition-all ${on ? 'start-[18px]' : 'start-0.5'}`} />
                </button>
                <span className="text-ink-2">{label}</span>
              </li>
            )
          })}
        </ul>
      </section>

      {st?.releases?.length ? (
        <section className="rounded-xl border border-line bg-panel-2 p-3 text-xs">
          <h3 className="mb-1.5 font-semibold">آخرین انتشارها</h3>
          {st.releases.map((r: any) => (
            <div key={r.id} className="flex justify-between py-0.5"><span className="ltr font-mono">{r.tag || r.id?.slice(0, 8)}</span>
              <span className={r.state === 'READY' ? 'text-ok' : r.state === 'FAILED' ? 'text-crit' : 'text-ink-3'}>{r.state}</span></div>
          ))}
        </section>
      ) : null}
    </div>
  )
}
