import { useState } from 'react'
import { CheckCircle2, Clock3, Loader2, ShieldAlert, ShieldCheck, Terminal, XCircle } from 'lucide-react'

export type Exec = { events: any[]; status: 'running' | 'ok' | 'failed' | 'rejected'; error?: string }

const ROWS: [string, string][] = [
  ['what', 'چه اتفاقی می‌افتد'], ['why', 'دلیل'], ['downtime', 'اثر روی در دسترس بودن'], ['cost', 'هزینه'],
  ['reversible', 'قابل بازگشت؟'], ['manual_steps', 'کار دستی شما'],
]

export function ApprovalCard({ a, exec, onApprove, onReject }: {
  a: any; exec?: Exec; onApprove: (confirm: string) => void; onReject: () => void
}) {
  const [confirm, setConfirm] = useState('')
  const critical = a.risk === 'critical'
  const decided = a.status !== 'pending' || exec
  const imp = a.impact || {}
  const fin = exec?.events.findLast((e) => e.type === 'done') || a.result
  const statusChip = fin?.release_ready && !fin?.ok
    ? <span className="inline-flex items-center gap-1 rounded-full bg-warn-soft px-2 py-0.5 text-[11px] text-warn"><ShieldAlert size={12} />منتشر شد؛ سایت خطا می‌دهد</span>
    : exec?.status === 'ok' || a.status === 'executed'
    ? <span className="inline-flex items-center gap-1 rounded-full bg-ok-soft px-2 py-0.5 text-[11px] text-ok"><CheckCircle2 size={12} />انجام و تأیید شد</span>
    : exec?.status === 'failed' || a.status === 'failed'
      ? <span className="inline-flex items-center gap-1 rounded-full bg-crit-soft px-2 py-0.5 text-[11px] text-crit"><XCircle size={12} />ناموفق</span>
      : exec?.status === 'rejected' || a.status === 'rejected'
        ? <span className="inline-flex items-center gap-1 rounded-full bg-panel-3 px-2 py-0.5 text-[11px] text-ink-3">رد شد — اجرا نشد</span>
        : exec?.status === 'running' || a.status === 'running'
          ? <span className="inline-flex items-center gap-1 rounded-full bg-ai-soft px-2 py-0.5 text-[11px] text-ai"><Loader2 size={12} className="animate-spin" />در حال اجرا</span>
          : <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] ${critical ? 'bg-crit-soft text-crit' : 'bg-warn-soft text-warn'}`}>
              <Clock3 size={12} />منتظر تأیید شما</span>

  return (
    <section className={`animate-fade-up overflow-hidden rounded-2xl border bg-panel shadow-card ${critical ? 'border-crit/60' : 'border-warn/50'}`}
      aria-label={`درخواست تأیید: ${a.title}`}>
      <header className={`flex items-center gap-2 px-4 py-2.5 ${critical ? 'bg-crit-soft' : 'bg-warn-soft'}`}>
        {critical ? <ShieldAlert size={17} className="text-crit" /> : <ShieldCheck size={17} className="text-warn" />}
        <h3 className="text-sm font-semibold">{a.title}</h3>
        <span className="text-[11px] text-ink-3">{critical ? 'عملیات بحرانی' : 'عملیات حساس'}</span>
        <div className="ms-auto">{statusChip}</div>
      </header>
      <div className="space-y-2 p-4 text-sm">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
          {ROWS.filter(([k]) => imp[k]).map(([k, label]) => (
            <div key={k} className="contents">
              <dt className="text-xs text-ink-3">{label}</dt>
              <dd className={k === 'reversible' && /خیر/.test(imp[k]) ? 'text-crit' : 'text-ink'}>{imp[k]}</dd>
            </div>
          ))}
        </dl>
        {imp.changes?.length ? (
          <table className="w-full text-xs">
            <thead className="text-ink-3"><tr><th className="text-start">متغیر</th><th className="text-start">مقدار (پوشانده)</th><th className="text-start">عملیات</th></tr></thead>
            <tbody>{imp.changes.map((c: any) => (
              <tr key={c.key} className="border-t border-line"><td className="py-1 font-mono ltr text-start">{c.key}</td>
                <td className="font-mono ltr text-start text-ink-2">{c.value}</td><td>{c.op}</td></tr>))}</tbody>
          </table>
        ) : null}
        {imp.warnings?.length ? (
          <ul className="space-y-1 text-xs text-warn">{imp.warnings.map((w: string, i: number) => <li key={i}>⚠ {w}</li>)}</ul>
        ) : null}

        {!decided && (
          <div className="flex flex-wrap items-center gap-2 pt-2">
            {critical && (
              <input value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={`برای تأیید، «${a.app}» را تایپ کنید`}
                className="ltr min-w-52 flex-1 rounded-lg border border-crit/50 bg-bg px-3 py-1.5 text-sm outline-none focus:border-crit" />
            )}
            <button onClick={() => onApprove(confirm)} disabled={critical && confirm.trim() !== a.app}
              className={`rounded-lg px-4 py-1.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-40 ${critical ? 'bg-crit text-white hover:brightness-110' : 'bg-accent text-bg hover:bg-accent-strong'}`}>
              تأیید و اجرا
            </button>
            <button onClick={onReject} className="rounded-lg border border-line-2 px-4 py-1.5 text-sm text-ink-2 hover:bg-panel-3">رد کردن</button>
            <span className="text-[11px] text-ink-3">تا تأیید نکنید هیچ تغییری اعمال نمی‌شود.</span>
          </div>
        )}
        {exec && exec.events.length > 0 && <ExecProgress exec={exec} />}
      </div>
    </section>
  )
}

function ExecProgress({ exec }: { exec: Exec }) {
  // Deploy events carry a `stage` (start event, then a `done` event); other actions emit sequential steps.
  const steps: { stage?: string; label: string; done: boolean; ok?: boolean }[] = []
  const logs: string[] = []
  for (const ev of exec.events) {
    if (ev.type === 'progress') {
      const s = ev.stage ? steps.find((x) => x.stage === ev.stage) : undefined
      if (s) {
        if (ev.done) { s.done = true; s.ok = ev.ok; s.label = `${s.label} — ${ev.label}` } else s.label = ev.label
      } else {
        if (!ev.stage) steps.forEach((x) => { x.done = true })
        steps.push({ stage: ev.stage, label: ev.label, done: !!ev.done, ok: ev.ok })
      }
    } else if (ev.type === 'build_log') logs.push(ev.line)
  }
  const final = exec.events.findLast((e) => e.type === 'done')
  if (final) {
    steps.forEach((s) => { s.done = true })
    if (!final.ok && steps.length) steps[steps.length - 1].ok = false
    if (final.error && !final.ok) steps.push({ label: `خطا: ${final.error}${final.reason ? ` — ${final.reason}` : ''}`, done: true, ok: false })
  }
  return (
    <div className="mt-2 rounded-xl border border-line bg-panel-2 p-3">
      <ol className="space-y-1.5">
        {steps.map((s, i) => (
          <li key={i} className="flex items-start gap-2 text-xs">
            {!s.done ? <Loader2 size={14} className="mt-0.5 animate-spin text-ai" />
              : s.ok === false ? <XCircle size={14} className="mt-0.5 text-crit" /> : <CheckCircle2 size={14} className="mt-0.5 text-ok" />}
            <span className={s.done ? 'text-ink-2' : 'text-ink'}>{s.label}</span>
          </li>
        ))}
      </ol>
      {logs.length > 0 && (
        <details className="mt-2" open={!final}>
          <summary className="flex cursor-pointer items-center gap-1 text-[11px] text-ink-3"><Terminal size={12} />لاگ ساخت ({logs.length} خط)</summary>
          <pre className="mt-1 max-h-48 overflow-auto rounded-lg bg-bg p-2 text-[11px] leading-5 text-ink-2 ltr text-left">{logs.slice(-60).join('\n')}</pre>
        </details>
      )}
      {final && !final.ok && (final.log_tail?.length || final.errors_after?.length) ? (
        <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-crit-soft p-2 text-[11px] leading-5 text-crit ltr text-left">
          {(final.log_tail || final.errors_after).slice(-8).join('\n')}</pre>
      ) : null}
    </div>
  )
}
