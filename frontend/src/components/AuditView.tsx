import { useEffect, useState } from 'react'
import { Bot, CheckCircle2, Clock3, Loader2, ShieldCheck, User, XCircle, Zap } from 'lucide-react'
import { getJSON } from '../api'

const STATUS: Record<string, { label: string; cls: string; icon: React.ReactNode }> = {
  ok: { label: 'انجام شد', cls: 'text-ink-2', icon: <CheckCircle2 size={13} className="text-ok" /> },
  success: { label: 'موفق', cls: 'text-ok', icon: <CheckCircle2 size={13} /> },
  failed: { label: 'ناموفق', cls: 'text-crit', icon: <XCircle size={13} /> },
  error: { label: 'خطا', cls: 'text-crit', icon: <XCircle size={13} /> },
  rejected: { label: 'رد شد', cls: 'text-ink-3', icon: <XCircle size={13} /> },
  approval_requested: { label: 'درخواست تأیید', cls: 'text-warn', icon: <Clock3 size={13} /> },
}
const ACTOR: Record<string, React.ReactNode> = {
  user: <span className="inline-flex items-center gap-1"><User size={12} />شما</span>,
  agent: <span className="inline-flex items-center gap-1 text-ai"><Bot size={12} />دستیار</span>,
  policy: <span className="inline-flex items-center gap-1 text-accent"><Zap size={12} />سیاست خودکار</span>,
}

export function AuditView() {
  const [rows, setRows] = useState<any[] | null>(null)
  useEffect(() => { getJSON('/api/audit?limit=200').then((d) => setRows(d.audit)).catch(() => setRows([])) }, [])
  return (
    <div className="mx-auto w-full max-w-5xl overflow-y-auto p-4">
      <h2 className="mb-1 flex items-center gap-2 text-base font-semibold"><ShieldCheck size={18} className="text-accent" />گزارش عملیات</h2>
      <p className="mb-3 text-xs text-ink-3">هر خواندن، هر درخواست تأیید و هر تغییر روی زیرساخت — چه کسی، چه زمانی، با چه نتیجه‌ای. مقادیر محرمانه پوشانده می‌شوند.</p>
      {!rows ? <Loader2 className="animate-spin" /> : (
        <div className="overflow-x-auto rounded-xl border border-line">
          <table className="w-full text-xs">
            <thead className="bg-panel-2 text-ink-3">
              <tr><th className="px-3 py-2 text-start">زمان</th><th className="text-start">عامل</th><th className="text-start">برنامه</th>
                <th className="text-start">عملیات</th><th className="text-start">سطح ریسک</th><th className="text-start">نتیجه</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const s = STATUS[r.status] || { label: r.status, cls: '', icon: null }
                return (
                  <tr key={r.id} className="border-t border-line">
                    <td className="num px-3 py-1.5 text-ink-3">{new Date(r.ts * 1000).toLocaleString('fa-IR')}</td>
                    <td>{ACTOR[r.actor] || r.actor}</td>
                    <td className="ltr text-start">{r.app || '—'}</td>
                    <td className="font-mono ltr text-start">{r.action}</td>
                    <td>{r.risk === 'read' ? <span className="text-ink-3">خواندنی</span> : r.risk === 'critical' ? <span className="text-crit">بحرانی</span> : <span className="text-warn">حساس</span>}</td>
                    <td><span className={`inline-flex items-center gap-1 ${s.cls}`}>{s.icon}{s.label}</span></td>
                  </tr>
                )
              })}
              {!rows.length && <tr><td colSpan={6} className="p-4 text-center text-ink-3">هنوز عملیاتی ثبت نشده است.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
