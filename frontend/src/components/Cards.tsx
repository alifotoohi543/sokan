import { useState } from 'react'
import {
  AlertTriangle, Ban, CheckCircle2, ChevronDown, CircleSlash, Cpu, FileSearch, Globe, HardDrive, Info, MemoryStick,
  Package, ScrollText, ServerCog, Sparkles, Stethoscope, Wrench, XCircle,
} from 'lucide-react'
import { UsageChart } from './Chart'
import { PLATFORM_FA, clock, n, n1, pct, toman } from '../fmt'

export function Box({ icon, title, children, tone = 'default', right }: {
  icon: React.ReactNode; title: string; children: React.ReactNode; tone?: 'default' | 'ai' | 'warn' | 'crit' | 'ok'; right?: React.ReactNode
}) {
  const border = { default: 'border-line', ai: 'border-ai/40', warn: 'border-warn/50', crit: 'border-crit/50', ok: 'border-ok/40' }[tone]
  return (
    <section className={`animate-fade-up overflow-hidden rounded-2xl border ${border} bg-panel shadow-card`}>
      <header className="flex items-center gap-2 border-b border-line px-4 py-2.5">
        <span className="text-ink-2">{icon}</span>
        <h3 className="text-sm font-semibold">{title}</h3>
        <div className="ms-auto">{right}</div>
      </header>
      <div className="p-4">{children}</div>
    </section>
  )
}

const confTone = (s: number) => (s >= 0.7 ? 'bg-crit-soft text-crit' : s >= 0.4 ? 'bg-warn-soft text-warn' : 'bg-panel-3 text-ink-2')

function Diagnosis({ d, onAsk }: { d: any; onAsk: (t: string) => void }) {
  const hs: any[] = d.hypotheses || []
  const s = d.summary || {}
  const [open, setOpen] = useState(0)
  const res = s.resources || {}
  return (
    <Box icon={<Stethoscope size={16} />} title={`گزارش عیب‌یابی — ${d.app}`} tone={d.healthy ? 'ok' : hs[0]?.score >= 0.7 ? 'crit' : 'warn'}
      right={<span className="text-xs text-ink-3">{n(s.log_lines)} خط لاگ بررسی شد</span>}>
      <div className="mb-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <Stat label="پاسخ سایت" value={s.http?.status ? `HTTP ${s.http.status}` : 'بدون پاسخ'}
          tone={!s.http?.status || s.http.status >= 500 ? 'crit' : 'ok'} sub={s.http?.latency_s != null ? `${n1(s.http.latency_s)} ثانیه` : ''} />
        <Stat label="RAM (بیشینه)" value={res.ram ? pct(res.ram.ratio * 100) : '—'} sub="از سقف پلن"
          tone={res.ram?.ratio >= 0.85 ? 'crit' : res.ram?.ratio >= 0.7 ? 'warn' : 'ok'} />
        <Stat label="CPU (بیشینه)" value={res.cpu ? pct(res.cpu.ratio * 100) : '—'} sub="از سقف پلن"
          tone={res.cpu?.ratio >= 0.9 ? 'crit' : res.cpu?.ratio >= 0.7 ? 'warn' : 'ok'} />
        <Stat label="آخرین انتشار" value={s.latest_release || '—'} tone={s.latest_release === 'READY' ? 'ok' : s.latest_release ? 'warn' : 'default'} />
      </div>
      {d.healthy && !hs.length ? (
        <p className="flex items-center gap-2 text-sm text-ok"><CheckCircle2 size={16} /> مشکلی پیدا نشد؛ سایت سالم پاسخ می‌دهد.</p>
      ) : (
        <ol className="space-y-2">
          {hs.map((h, i) => (
            <li key={h.id} className="rounded-xl border border-line bg-panel-2">
              <button className="flex w-full items-center gap-2 px-3 py-2 text-start" onClick={() => setOpen(open === i ? -1 : i)}
                aria-expanded={open === i}>
                <span className="grid size-6 shrink-0 place-items-center rounded-full bg-panel-3 text-xs num">{n(i + 1)}</span>
                <span className="flex-1 text-sm font-medium">{h.title}</span>
                <span className={`rounded-full px-2 py-0.5 text-[11px] ${confTone(h.score)}`}>احتمال {h.confidence} · {pct(h.score * 100)}</span>
                <ChevronDown size={16} className={`text-ink-3 transition ${open === i ? 'rotate-180' : ''}`} />
              </button>
              {open === i && (
                <div className="space-y-2 border-t border-line px-3 py-2.5">
                  <div className="text-xs text-ink-3">شواهد:</div>
                  <ul className="space-y-1">
                    {h.evidence.map((e: string, j: number) => /[؀-ۿ]/.test(e) ? (
                      <li key={j} className="flex gap-1.5 px-1 text-[12.5px] leading-6 text-ink-2"><span className="text-accent">•</span>{e}</li>
                    ) : (
                      <li key={j} className="rounded-md bg-bg px-2 py-1 font-mono text-[11.5px] leading-5 text-ink-2 ltr text-left break-all">{e}</li>
                    ))}
                  </ul>
                  {h.fixes?.length ? (
                    <div className="flex flex-wrap gap-2 pt-1">
                      {h.fixes.map((f: any, j: number) => (
                        <button key={j} onClick={() => onAsk(fixPrompt(d.app, f, h))}
                          className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs transition hover:bg-panel-3 ${f.kind === 'guide' ? 'border-line text-ink-2' : 'border-accent/50 text-accent'}`}>
                          {f.kind === 'guide' ? <Info size={13} /> : <Wrench size={13} />}{f.label}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
      {d.series && (d.series.mem_gb?.length > 1 || d.series.cpu?.length > 1) ? (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <UsageChart title="مصرف RAM" unit="MB" points={(d.series.mem_gb || []).map((p: number[]) => [p[0], p[1] * 1024])}
            limit={res.limits?.ram_gb ? res.limits.ram_gb * 1024 : null} height={100} />
          <UsageChart title="مصرف CPU" unit="هسته" points={d.series.cpu || []} limit={res.limits?.cpu} height={100} />
        </div>
      ) : null}
    </Box>
  )
}

function fixPrompt(app: string, f: any, h: any): string {
  if (f.action === 'set_env') {
    const keys = (f.params?.keys || []).join('، ')
    return `برای ${app} متغیر ${keys} را تنظیم کن (مقدارش را الان می‌گویم): `
  }
  if (f.kind === 'guide') return `درباره‌ی «${f.label}» برای ${app} دقیق‌تر راهنمایی کن`
  return `${f.label} را برای ${app} انجام بده (علت: ${h.title})`
}

export function Stat({ label, value, sub, tone = 'default' }: { label: string; value: React.ReactNode; sub?: string; tone?: 'default' | 'ok' | 'warn' | 'crit' }) {
  const c = { default: 'text-ink', ok: 'text-ok', warn: 'text-warn', crit: 'text-crit' }[tone]
  const Icon = { default: null, ok: CheckCircle2, warn: AlertTriangle, crit: XCircle }[tone]
  return (
    <div className="rounded-xl border border-line bg-panel-2 px-3 py-2">
      <div className="text-[11px] text-ink-3">{label}</div>
      <div className={`num mt-0.5 flex items-center gap-1 text-sm font-semibold ${c}`}>{Icon ? <Icon size={13} aria-hidden /> : null}{value}</div>
      {sub ? <div className="text-[11px] text-ink-3">{sub}</div> : null}
    </div>
  )
}

function Metrics({ d }: { d: any }) {
  return (
    <Box icon={<Cpu size={16} />} title={`مصرف منابع — ${d.app} (${n(d.minutes)} دقیقه‌ی اخیر)`}>
      <div className="grid gap-2 sm:grid-cols-2">
        <UsageChart title="RAM" unit="MB" points={d.series?.mem_mb || []} limit={d.limits?.ram_gb ? d.limits.ram_gb * 1024 : null} />
        <UsageChart title="CPU" unit="هسته" points={d.series?.cpu || []} limit={d.limits?.cpu} />
      </div>
      <p className="mt-2 text-xs text-ink-3">
        RAM: بیشینه {d.ram ? `${n(d.ram.max_mb)}MB (${pct(d.ram.pct_of_limit)} سقف) · روند ${d.ram.trend === 'rising' ? 'صعودی ⚠' : 'پایدار'}` : '—'}
        {' · '}CPU: بیشینه {d.cpu ? `${pct(d.cpu.pct_of_limit)} سقف` : '—'}
      </p>
    </Box>
  )
}

function Logs({ d }: { d: any }) {
  const [all, setAll] = useState(false)
  const lines: any[] = all ? d.lines : d.lines.slice(-14)
  const isErr = (l: string) => /error|exception|traceback|fatal|killed|keyerror|\b5\d\d\b/i.test(l)
  return (
    <Box icon={<ScrollText size={16} />} title={`لاگ‌های ${d.app}`}
      right={<span className="text-xs text-ink-3">{n(d.total)} خط · <span className={d.error_like ? 'text-crit' : ''}>{n(d.error_like)} خطا</span></span>}>
      {lines.length ? (
        <pre className="max-h-72 overflow-auto rounded-lg bg-bg p-2 text-[11.5px] leading-5 ltr text-left">
          {lines.map((l, i) => (
            <div key={i} className={isErr(l.line) ? 'text-crit' : 'text-ink-2'}>
              <span className="text-ink-3">{new Date(l.ts * 1000).toLocaleTimeString('en-GB')} </span>{l.line}
            </div>
          ))}
        </pre>
      ) : <p className="text-sm text-ink-3">در این بازه لاگی ثبت نشده است.</p>}
      {d.lines.length > 14 && (
        <button className="mt-2 text-xs text-accent" onClick={() => setAll(!all)}>{all ? 'نمایش کمتر' : `نمایش همه (${n(d.lines.length)})`}</button>
      )}
    </Box>
  )
}

function AppStatus({ d }: { d: any }) {
  const http = d.http || {}
  return (
    <Box icon={<ServerCog size={16} />} title={`وضعیت ${d.name}`}
      right={<a href={d.url} target="_blank" rel="noreferrer" className="text-xs text-accent ltr">{d.url?.replace('https://', '')}</a>}>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="وضعیت" value={d.scale === 0 ? 'خاموش' : d.status || '—'} tone={d.scale === 0 ? 'warn' : 'ok'} />
        <Stat label="پاسخ سایت" value={http.status ? `HTTP ${http.status}` : 'بدون پاسخ'} sub={http.latency_s != null ? `${n1(http.latency_s)} ثانیه` : ''}
          tone={!http.status || http.status >= 500 ? 'crit' : http.latency_s > 5 ? 'warn' : 'ok'} />
        <Stat label="پلن" value={d.plan || '—'} sub={d.limits ? `${n1(d.limits.ram_gb)}GB RAM · ${n1(d.limits.cpu)} CPU` : ''} />
        <Stat label="پلتفرم" value={PLATFORM_FA[d.platform] || d.platform || '—'} sub={`${n(d.env_keys?.length)} متغیر محیطی`} />
      </div>
      {d.releases?.length ? (
        <div className="mt-3 text-xs text-ink-2">
          آخرین انتشارها: {d.releases.map((r: any) => `${r.tag || r.id?.slice(0, 6)} (${r.state})`).join(' · ')}
        </div>
      ) : null}
    </Box>
  )
}

function ProjectAnalysis({ d, onAsk }: { d: any; onAsk: (t: string) => void }) {
  const rec = d.recommendation || {}
  return (
    <Box icon={<FileSearch size={16} />} title="تحلیل پروژه و پیشنهاد زیرساخت" tone="ai">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="پلتفرم" value={d.framework || PLATFORM_FA[d.platform] || d.platform} />
        <Stat label="پورت" value={<span className="ltr">{d.port}</span>} />
        <Stat label="حجم سورس" value={`${n1(d.size_mb)} MB`} />
        <Stat label="متغیرهای لازم" value={n(d.required_envs?.length)} sub={(d.required_envs || []).join(', ')} />
      </div>
      {rec.ok && (
        <div className="mt-3 rounded-xl border border-accent/40 bg-accent-soft p-3">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-sm font-semibold text-accent">پیشنهاد: پلن {rec.plan.id} + بسته‌ی {rec.bundle_id}</span>
            <span className="num text-sm">{toman(rec.monthly_toman)} در ماه</span>
            <span className="num text-xs text-ink-3">(~{toman(rec.daily_toman)} در روز)</span>
          </div>
          <ul className="mt-2 list-disc space-y-0.5 ps-5 text-xs text-ink-2">{rec.why.map((w: string, i: number) => <li key={i}>{w}</li>)}</ul>
        </div>
      )}
      {d.warnings?.length ? (
        <ul className="mt-3 space-y-1">
          {d.warnings.map((w: string, i: number) => (
            <li key={i} className="flex gap-2 text-xs text-warn"><AlertTriangle size={14} className="mt-0.5 shrink-0" />{w}</li>
          ))}
        </ul>
      ) : null}
      <ul className="mt-2 space-y-0.5 text-[11px] text-ink-3">{d.evidence.map((e: string, i: number) => <li key={i}>• {e}</li>)}</ul>
      {rec.ok && (
        <button onClick={() => onAsk(`یک برنامه با نام مناسب روی پلن ${rec.plan.id} و بسته‌ی ${rec.bundle_id} بساز و پروژه‌ی ${d.path} را روی آن دیپلوی کن`)}
          className="mt-3 inline-flex items-center gap-2 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-bg hover:bg-accent-strong">
          <Package size={15} /> ساخت و دیپلوی با این پیشنهاد
        </button>
      )}
    </Box>
  )
}

function Overview({ d }: { d: any }) {
  return (
    <Box icon={<Sparkles size={16} />} title="نمای کلی حساب">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <Stat label="اعتبار" value={toman(d.balance_toman)} />
        <Stat label="برنامه‌ها" value={n(d.apps?.length)} />
        <Stat label="هشدارهای باز" value={n(d.open_alerts?.length)} tone={d.open_alerts?.length ? 'warn' : 'ok'} />
      </div>
    </Box>
  )
}

function Plans({ d }: { d: any }) {
  return (
    <Box icon={<HardDrive size={16} />} title="پلن‌های فعال لیارا (منطقه‌ی ایران)">
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-ink-3"><tr><th className="py-1 text-start">پلن</th><th>RAM</th><th>CPU</th><th>دیسک</th><th className="text-end">ماهانه</th></tr></thead>
          <tbody className="num">
            {(d.plans || []).filter((p: any) => p.g2).map((p: any) => (
              <tr key={p.id} className="border-t border-line"><td className="py-1 ltr text-start">{p.id}</td><td className="text-center">{n1(p.ram_gb)}GB</td>
                <td className="text-center">{n1(p.cpu)}</td><td className="text-center">{n(p.disk_gb)}GB</td><td className="text-end">{toman(p.monthly_toman)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </Box>
  )
}

export function Capabilities({ d }: { d: any }) {
  return (
    <Box icon={<CircleSlash size={16} />} title="چه کارهایی از طریق API لیارا ممکن نیست">
      <ul className="space-y-2">
        {d.cannot.map((c: any, i: number) => (
          <li key={i} className="rounded-xl border border-line bg-panel-2 p-2.5 text-xs">
            <div className="flex items-center gap-1.5 font-semibold text-ink"><Ban size={13} className="text-warn" />{c.item}</div>
            <div className="mt-1 text-ink-3">چرا: {c.why}</div>
            <div className="mt-0.5 text-ink-2">راه جایگزین: {c.workaround}</div>
          </li>
        ))}
      </ul>
    </Box>
  )
}

function Releases({ d }: { d: any }) {
  return (
    <Box icon={<Package size={16} />} title="تاریخچه‌ی نسخه‌ها">
      <ul className="space-y-1 text-xs">
        {d.releases.map((r: any) => (
          <li key={r.id} className="flex items-center gap-2 rounded-lg bg-panel-2 px-2 py-1.5">
            <span className="ltr font-mono">{r.tag || r.id?.slice(0, 8)}</span>
            <span className={r.state === 'READY' ? 'text-ok' : r.state === 'FAILED' ? 'text-crit' : 'text-ink-3'}>{r.state}</span>
            {r.id === d.current && <span className="rounded bg-accent-soft px-1.5 text-accent">فعلی</span>}
            <span className="ms-auto text-ink-3">{r.created ? clock(Date.parse(r.created) / 1000) : ''}</span>
          </li>
        ))}
      </ul>
    </Box>
  )
}

function Domains({ d }: { d: any }) {
  return (
    <Box icon={<Globe size={16} />} title="دامنه‌ها">
      {d.domains.length ? d.domains.map((x: any) => (
        <div key={x.id} className="text-xs">{x.name} → {x.app} · {x.status} · SSL: {x.ssl} {x.cname_target ? `· CNAME: ${x.cname_target}` : ''}</div>
      )) : <p className="text-xs text-ink-3">هنوز دامنه‌ای متصل نشده است.</p>}
    </Box>
  )
}

function AppsList({ d }: { d: any }) {
  return (
    <Box icon={<MemoryStick size={16} />} title="برنامه‌ها">
      <ul className="text-xs">{d.apps.map((a: any) => <li key={a.name}>{a.name} · {a.plan} · {a.status}</li>)}</ul>
    </Box>
  )
}

export function Card({ kind, data, onAsk }: { kind: string; data: any; onAsk: (t: string) => void }) {
  switch (kind) {
    case 'diagnose': return <Diagnosis d={data} onAsk={onAsk} />
    case 'get_metrics': return <Metrics d={data} />
    case 'get_logs': return <Logs d={data} />
    case 'app_status': return <AppStatus d={data} />
    case 'analyze_project': return data.ok === false ? null : <ProjectAnalysis d={data} onAsk={onAsk} />
    case 'account_overview': return <Overview d={data} />
    case 'list_plans': return <Plans d={data} />
    case 'api_capabilities': return <Capabilities d={data} />
    case 'list_releases': return <Releases d={data} />
    case 'domain_status': return <Domains d={data} />
    case 'list_apps': return <AppsList d={data} />
    default: return null
  }
}
