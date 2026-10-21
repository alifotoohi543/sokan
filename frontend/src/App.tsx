import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity, AlertTriangle, Bot, CheckCircle2, CircleDot, ClipboardList, Loader2, Plus, RefreshCw, Rocket, SendHorizonal,
  ShieldCheck, Sparkles, Wallet, XCircle,
} from 'lucide-react'
import { getJSON, postJSON, streamPost } from './api'
import { Capabilities, Card } from './components/Cards'
import { ApprovalCard, type Exec } from './components/Approval'
import { AppPanel } from './components/AppPanel'
import { AuditView } from './components/AuditView'
import { ago, toman } from './fmt'

type Step = { id: string; label: string; status: 'running' | 'done' | 'error'; summary?: string }
type Turn = { id: string; role: 'user' | 'assistant'; text?: string; steps: Step[]; cards: { kind: string; data: any }[]; approvals: string[]; busy?: boolean }

const SUGGESTIONS = [
  { icon: <XCircle size={14} />, text: 'سایتم ارور ۵۰۰ می‌ده، مشکلش چیه؟' },
  { icon: <Activity size={14} />, text: 'چرا سایتم کند شده؟ مصرف منابع رو بررسی کن' },
  { icon: <Rocket size={14} />, text: 'پروژه‌ی C:\\Hackaton\\liara-copilot\\demo-app رو بررسی کن و بگو چه پلنی لازم داره' },
  { icon: <ClipboardList size={14} />, text: 'لاگ‌های نیم ساعت اخیر رو نشون بده' },
  { icon: <ShieldCheck size={14} />, text: 'چه کارهایی از طریق API لیارا از دستت برنمیاد؟' },
]

const uid = () => Math.random().toString(36).slice(2, 10)

function getSession(): string {
  try {
    const s = localStorage.getItem('sokan-session')
    if (s) return s
  } catch { /* storage unavailable */ }
  return ''
}

export default function App() {
  const [ov, setOv] = useState<any>(null)
  const [ovErr, setOvErr] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [approvals, setApprovals] = useState<Record<string, any>>({})
  const [execs, setExecs] = useState<Record<string, Exec>>({})
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [session, setSession] = useState(getSession)
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState<'chat' | 'audit' | 'limits'>('chat')
  const [toasts, setToasts] = useState<{ id: string; title: string; detail: string; tone: string }[]>([])
  const [caps, setCaps] = useState<any>(null)
  const [panelKey, setPanelKey] = useState(0)
  const bottom = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const loadOverview = useCallback(async () => {
    try {
      const d = await getJSON('/api/overview')
      setOv(d)
      setOvErr('')
      setSelected((s) => s ?? d.apps?.[0]?.name ?? null)
    } catch (e: any) { setOvErr(e.message) }
  }, [])

  useEffect(() => {
    loadOverview()
    const t = setInterval(loadOverview, 20000)
    return () => clearInterval(t)
  }, [loadOverview])

  // Restore approvals still waiting for a decision (e.g. after a page reload) so nothing is silently lost.
  useEffect(() => {
    getJSON('/api/approvals').then((d) => {
      const pend: any[] = d.pending || []
      if (!pend.length) return
      setApprovals((a) => Object.fromEntries([...Object.entries(a), ...pend.map((p) => [p.id, p])]))
      setTurns((ts) => [...ts, {
        id: uid(), role: 'assistant', steps: [], cards: [], approvals: pend.map((p) => p.id),
        text: `${pend.length.toLocaleString('fa-IR')} درخواست از قبل منتظر تصمیم شماست.`,
      }])
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const es = new EventSource('/api/events')
    es.onmessage = (m) => {
      try {
        const ev = JSON.parse(m.data)
        if (ev.type === 'alert' || ev.type === 'auto_action') {
          const id = uid()
          setToasts((t) => [...t, { id, title: ev.title, detail: ev.detail, tone: ev.type === 'auto_action' ? 'ai' : ev.severity }])
          setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 9000)
          loadOverview()
        }
        if (ev.type === 'health') { loadOverview(); setPanelKey((k) => k + 1) }
      } catch { /* ignore */ }
    }
    return () => es.close()
  }, [loadOverview])

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [turns, execs])

  const patchTurn = (id: string, fn: (t: Turn) => Turn) => setTurns((ts) => ts.map((t) => (t.id === id ? fn(t) : t)))

  /** Apply one agent event (step/card/approval/message/error) to an assistant turn. */
  function applyAgentEvent(tid: string, ev: any) {
    if (ev.type === 'step') {
      patchTurn(tid, (t) => {
        const idx = t.steps.findIndex((s) => s.id === ev.id)
        const st: Step = { id: ev.id, label: ev.label, status: ev.status, summary: ev.summary }
        if (idx >= 0) { const c = [...t.steps]; c[idx] = st; return { ...t, steps: c } }
        return { ...t, steps: [...t.steps, st] }
      })
    } else if (ev.type === 'card') {
      patchTurn(tid, (t) => ({ ...t, cards: [...t.cards, { kind: ev.kind, data: ev.data }] }))
    } else if (ev.type === 'approval') {
      setApprovals((a) => ({ ...a, [ev.approval.id]: ev.approval }))
      patchTurn(tid, (t) => ({ ...t, approvals: [...t.approvals, ev.approval.id] }))
    } else if (ev.type === 'message') {
      patchTurn(tid, (t) => ({ ...t, text: (t.text ? t.text + '\n\n' : '') + ev.text }))
    } else if (ev.type === 'error') {
      patchTurn(tid, (t) => ({ ...t, text: `⚠️ ${ev.text}` }))
    }
  }

  async function send(text: string) {
    const q = text.trim()
    if (!q || busy) return
    setTab('chat')
    setInput('')
    setBusy(true)
    const aid = uid()
    setTurns((ts) => [...ts, { id: uid(), role: 'user', text: q, steps: [], cards: [], approvals: [] },
      { id: aid, role: 'assistant', steps: [], cards: [], approvals: [], busy: true }])
    try {
      await streamPost('/api/chat', { text: q, session, app: selected || '' }, (ev) => {
        if (ev.type === 'session') {
          setSession(ev.session)
          try { localStorage.setItem('sokan-session', ev.session) } catch { /* ignore */ }
        } else applyAgentEvent(aid, ev)
      })
    } catch (e: any) {
      patchTurn(aid, (t) => ({ ...t, text: `⚠️ ارتباط با سرور برقرار نشد: ${e.message}` }))
    } finally {
      patchTurn(aid, (t) => ({ ...t, busy: false }))
      setBusy(false)
      inputRef.current?.focus()
    }
  }

  async function approve(id: string, confirm: string) {
    setExecs((x) => ({ ...x, [id]: { events: [], status: 'running' } }))
    let follow: string | null = null
    setBusy(true)
    try {
      await streamPost(`/api/approvals/${id}/approve`, { confirm_text: confirm, session }, (ev) => {
        if (ev.type === 'exec') {
          setExecs((x) => {
            const cur = x[id] || { events: [], status: 'running' }
            const done = ev.event.type === 'done'
            return { ...x, [id]: { events: [...cur.events, ev.event], status: done ? (ev.event.ok ? 'ok' : 'failed') : cur.status } }
          })
        } else if (ev.type === 'message' && ev.approval_id) {
          setTurns((ts) => [...ts, { id: uid(), role: 'assistant', text: ev.text, steps: [], cards: [], approvals: [] }])
        } else if (ev.type === 'approval_update') {
          setApprovals((a) => ({ ...a, [id]: ev.approval }))
        } else if (ev.type === 'followup_start') {
          const fid = uid()
          follow = fid
          setTurns((ts) => [...ts, { id: fid, role: 'assistant', steps: [], cards: [], approvals: [], busy: true }])
        } else if (ev.type === 'error' && !follow) {
          setExecs((x) => ({ ...x, [id]: { ...(x[id] || { events: [] }), status: 'failed', error: ev.text } }))
        } else if (follow) {
          applyAgentEvent(follow, ev)
        }
      })
    } catch (e: any) {
      setExecs((x) => ({ ...x, [id]: { events: [{ type: 'done', ok: false, error: e.message }], status: 'failed' } }))
    } finally {
      if (follow) {
        const f = follow
        // drop an empty follow-up bubble (agent had nothing to add)
        setTurns((ts) => ts.flatMap((t) => (t.id !== f ? [t] : (t.text || t.steps.length || t.cards.length || t.approvals.length) ? [{ ...t, busy: false }] : [])))
      }
      setBusy(false)
    }
    loadOverview()
    setPanelKey((k) => k + 1)
  }

  async function reject(id: string) {
    const r = await postJSON(`/api/approvals/${id}/reject`, { session })
    if (r.approval) setApprovals((a) => ({ ...a, [id]: r.approval }))
    setExecs((x) => ({ ...x, [id]: { events: [], status: 'rejected' } }))
  }

  useEffect(() => { if (tab === 'limits' && !caps) getJSON('/api/capabilities').then(setCaps).catch(() => {}) }, [tab, caps])

  const alertsByApp: Record<string, any[]> = {}
  for (const a of ov?.open_alerts || []) (alertsByApp[a.app] ||= []).push(a)
  const llm = ov?.llm

  return (
    <div className="flex h-full flex-col">
      {/* ------------------------------------------------ header */}
      <header className="flex flex-wrap items-center gap-3 border-b border-line bg-panel px-4 py-2.5">
        <div className="flex items-center gap-2">
          <img src="/favicon.svg" alt="" className="size-8" />
          <div className="leading-tight">
            <div className="text-[15px] font-bold">سکان</div>
            <div className="text-[11px] text-ink-3">همکار فنی شما روی لیارا</div>
          </div>
        </div>
        <nav className="ms-4 flex gap-1 rounded-xl bg-panel-2 p-1 text-sm" aria-label="بخش‌ها">
          {([['chat', 'دستیار'], ['audit', 'گزارش عملیات'], ['limits', 'محدودیت‌های API']] as const).map(([k, l]) => (
            <button key={k} onClick={() => setTab(k)} aria-current={tab === k}
              className={`rounded-lg px-3 py-1 transition ${tab === k ? 'bg-panel-3 text-ink' : 'text-ink-3 hover:text-ink'}`}>{l}</button>
          ))}
        </nav>
        <div className="ms-auto flex flex-wrap items-center gap-2 text-xs">
          {ov?.mode === 'mock' && <span className="rounded-full bg-warn-soft px-2.5 py-1 text-warn">حالت شبیه‌ساز (آزمایشی)</span>}
          <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-panel-2 px-2.5 py-1">
            <Wallet size={13} className="text-accent" />
            {ov?.workspace?.team_name ? `${ov.workspace.team_name} · ` : ''}<span className="num">{toman(ov?.balance_toman)}</span>
          </span>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-panel-2 px-2.5 py-1" title="پایش خودکار هر ۶۰ ثانیه">
            <CircleDot size={12} className={ov?.monitor?.error ? 'text-crit' : 'animate-pulse-dot text-ok'} />
            پایش خودکار {ov?.monitor?.last_run ? `· ${ago(ov.monitor.last_run)}` : ''}
          </span>
          {llm && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-panel-2 px-2.5 py-1" title="بودجه‌ی مدل زبانی">
              <Bot size={13} className="text-ai" /> <span className="num">${llm.usd.toFixed(3)} / ${llm.cap}</span>
            </span>
          )}
        </div>
      </header>
      {ovErr && <div className="bg-crit-soft px-4 py-1.5 text-xs text-crit">⚠️ {ovErr}</div>}

      <div className="flex min-h-0 flex-1">
        {/* ------------------------------------------------ sidebar: apps + alerts */}
        <aside className="hidden w-64 shrink-0 flex-col gap-3 overflow-y-auto border-e border-line bg-panel p-3 md:flex" aria-label="برنامه‌ها">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold text-ink-3">برنامه‌های شما</h2>
            <button onClick={loadOverview} className="text-ink-3 hover:text-ink" aria-label="به‌روزرسانی"><RefreshCw size={14} /></button>
          </div>
          <ul className="space-y-1">
            {(ov?.apps || []).map((a: any) => {
              const al = alertsByApp[a.name] || []
              const crit = al.some((x) => x.severity === 'critical')
              const tone = a.scale === 0 ? 'text-ink-3' : crit ? 'text-crit' : al.length ? 'text-warn' : 'text-ok'
              const label = a.scale === 0 ? 'خاموش' : crit ? 'مشکل جدی' : al.length ? 'هشدار' : 'سالم'
              return (
                <li key={a.name}>
                  <button onClick={() => setSelected(a.name)} aria-current={selected === a.name}
                    className={`flex w-full items-center gap-2 rounded-xl border px-2.5 py-2 text-start transition ${selected === a.name ? 'border-accent/50 bg-accent-soft' : 'border-transparent hover:bg-panel-2'}`}>
                    <span className={`flex items-center gap-1 text-[11px] ${tone}`}>
                      {crit ? <XCircle size={13} /> : al.length ? <AlertTriangle size={13} /> : <CheckCircle2 size={13} />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm ltr text-start">{a.name}</span>
                      <span className="block text-[11px] text-ink-3">{label} · {a.plan}</span>
                    </span>
                  </button>
                </li>
              )
            })}
            {ov && !ov.apps?.length && <li className="rounded-xl border border-dashed border-line p-3 text-xs text-ink-3">هنوز برنامه‌ای ندارید.</li>}
          </ul>
          <button onClick={() => { setInput('سایت جدیدم رو می‌خوام روی لیارا بالا بیارم. پروژه در مسیر C:\\Hackaton\\liara-copilot\\demo-app هست'); inputRef.current?.focus() }}
            className="inline-flex items-center justify-center gap-1.5 rounded-xl border border-dashed border-accent/50 px-3 py-2 text-sm text-accent hover:bg-accent-soft">
            <Plus size={15} /> راه‌اندازی سایت جدید
          </button>

          <h2 className="mt-2 text-xs font-semibold text-ink-3">هشدارهای باز</h2>
          <ul className="space-y-1.5">
            {(ov?.open_alerts || []).map((a: any, i: number) => (
              <li key={i}>
                <button onClick={() => send(`${a.title}. بررسی کن مشکل چیه`)}
                  className={`w-full rounded-xl border p-2 text-start text-xs transition hover:brightness-125 ${a.severity === 'critical' ? 'border-crit/40 bg-crit-soft' : 'border-warn/40 bg-warn-soft'}`}>
                  <span className={`flex items-center gap-1 font-medium ${a.severity === 'critical' ? 'text-crit' : 'text-warn'}`}>
                    <AlertTriangle size={12} />{a.severity === 'critical' ? 'بحرانی' : 'هشدار'}</span>
                  <span className="mt-0.5 block text-ink">{a.title}</span>
                  <span className="mt-1 block text-[11px] text-ink-3">برای عیب‌یابی کلیک کنید</span>
                </button>
              </li>
            ))}
            {ov && !ov.open_alerts?.length && <li className="text-xs text-ink-3">همه‌چیز آرام است ✓</li>}
          </ul>
        </aside>

        {/* ------------------------------------------------ main */}
        <main className="flex min-w-0 flex-1 flex-col">
          {tab === 'audit' ? <AuditView /> : tab === 'limits' ? (
            <div className="mx-auto w-full max-w-3xl overflow-y-auto p-4">{caps ? <Capabilities d={caps} /> : <Loader2 className="animate-spin" />}</div>
          ) : (
            <>
              <div className="flex-1 overflow-y-auto">
                <div className="mx-auto max-w-3xl space-y-4 px-4 py-6">
                  {turns.length === 0 && <Welcome onPick={send} />}
                  {turns.map((t) => t.role === 'user' ? (
                    <div key={t.id} className="flex justify-start">
                      <div className="max-w-[85%] rounded-2xl rounded-ss-md bg-accent-soft px-4 py-2.5 text-sm leading-7 text-ink">{t.text}</div>
                    </div>
                  ) : (
                    <div key={t.id} className="space-y-3">
                      {t.steps.length > 0 && (
                        <ol className="space-y-1 rounded-2xl border border-line bg-panel/60 px-4 py-2.5" aria-label="مراحل بررسی">
                          {t.steps.map((s) => (
                            <li key={s.id} className="flex items-center gap-2 text-xs">
                              {s.status === 'running' ? <Loader2 size={13} className="animate-spin text-ai" />
                                : s.status === 'error' ? <XCircle size={13} className="text-crit" /> : <CheckCircle2 size={13} className="text-ok" />}
                              <span className={s.status === 'running' ? 'text-ink' : 'text-ink-2'}>{s.label}</span>
                              {s.summary && <span className="text-crit">— {s.summary}</span>}
                            </li>
                          ))}
                        </ol>
                      )}
                      {t.cards.map((c, i) => <Card key={i} kind={c.kind} data={c.data} onAsk={(q) => { setInput(q); inputRef.current?.focus() }} />)}
                      {t.approvals.map((id) => approvals[id] && (
                        <ApprovalCard key={id} a={approvals[id]} exec={execs[id]} onApprove={(c) => approve(id, c)} onReject={() => reject(id)} />
                      ))}
                      {(t.text || t.busy) && (
                        <div className="flex gap-2.5">
                          <span className="mt-1 grid size-7 shrink-0 place-items-center rounded-full bg-ai-soft text-ai"><Sparkles size={14} /></span>
                          <div className="min-w-0 flex-1 whitespace-pre-wrap rounded-2xl rounded-se-md border border-line bg-panel px-4 py-2.5 text-sm leading-7">
                            {t.text ? <Md text={t.text} /> : <span className="inline-flex items-center gap-2 text-ink-3"><Loader2 size={14} className="animate-spin" />در حال بررسی...</span>}
                          </div>
                        </div>
                      )}
                    </div>
                  ))}
                  <div ref={bottom} />
                </div>
              </div>
              {/* composer */}
              <div className="border-t border-line bg-panel px-4 py-3">
                <form className="mx-auto flex max-w-3xl items-end gap-2" onSubmit={(e) => { e.preventDefault(); send(input) }}>
                  <textarea ref={inputRef} value={input} onChange={(e) => setInput(e.target.value)} rows={1}
                    onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input) } }}
                    placeholder="مشکل یا درخواستتان را ساده بنویسید؛ مثلاً «سایتم کند شده»"
                    aria-label="پیام به دستیار"
                    className="max-h-40 min-h-11 flex-1 resize-none rounded-xl border border-line-2 bg-bg px-3.5 py-2.5 text-sm outline-none placeholder:text-ink-3 focus:border-accent" />
                  <button type="submit" disabled={busy || !input.trim()} aria-label="ارسال"
                    className="grid size-11 place-items-center rounded-xl bg-accent text-bg transition hover:bg-accent-strong disabled:opacity-40">
                    {busy ? <Loader2 size={18} className="animate-spin" /> : <SendHorizonal size={18} className="-scale-x-100" />}
                  </button>
                </form>
                <p className="mx-auto mt-1.5 max-w-3xl text-[11px] text-ink-3">
                  خواندن اطلاعات خودکار است؛ هر تغییری (ری‌استارت، تنظیمات، دیپلوی، پلن) فقط بعد از تأیید شما اجرا می‌شود.
                </p>
              </div>
            </>
          )}
        </main>

        {/* ------------------------------------------------ app panel */}
        {selected && (
          <aside className="hidden w-[22rem] shrink-0 overflow-y-auto border-s border-line bg-panel xl:block" aria-label="داشبورد برنامه">
            <AppPanel key={selected + panelKey} app={selected} catalog={ov?.policies_catalog || {}} onAsk={send} />
          </aside>
        )}
      </div>

      {/* toasts */}
      <div className="pointer-events-none fixed bottom-24 left-4 z-50 flex w-80 flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`pointer-events-auto animate-fade-up rounded-xl border p-3 text-sm shadow-card ${t.tone === 'critical' ? 'border-crit/50 bg-crit-soft' : t.tone === 'ai' ? 'border-ai/50 bg-ai-soft' : 'border-warn/50 bg-warn-soft'}`}>
            <div className="flex items-center gap-1.5 font-semibold">
              {t.tone === 'ai' ? <Bot size={14} className="text-ai" /> : <AlertTriangle size={14} className={t.tone === 'critical' ? 'text-crit' : 'text-warn'} />}
              {t.title}
            </div>
            <div className="mt-0.5 text-xs text-ink-2">{t.detail}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function Welcome({ onPick }: { onPick: (t: string) => void }) {
  return (
    <div className="animate-fade-up py-8 text-center">
      <img src="/favicon.svg" alt="" className="mx-auto size-14" />
      <h1 className="mt-3 text-xl font-bold">سلام! من سکان هستم</h1>
      <p className="mx-auto mt-2 max-w-md text-sm leading-7 text-ink-2">
        همکار فنی شما روی لیارا. سایت را راه‌اندازی می‌کنم، دائم سلامتش را پایش می‌کنم و اگر مشکلی پیش آمد علتش را با شواهد پیدا می‌کنم.
        هر تغییری فقط با تأیید شما انجام می‌شود.
      </p>
      <div className="mx-auto mt-6 grid max-w-xl gap-2 text-start sm:grid-cols-2">
        {SUGGESTIONS.map((s) => (
          <button key={s.text} onClick={() => onPick(s.text)}
            className="flex items-center gap-2 rounded-xl border border-line bg-panel px-3 py-2.5 text-sm text-ink-2 transition hover:border-accent/50 hover:text-ink">
            <span className="text-accent">{s.icon}</span><span className="flex-1">{s.text}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

/** Minimal markdown: **bold**, `code`, bullet lines. */
function Md({ text }: { text: string }) {
  return (
    <>
      {text.split('\n').map((line, i) => {
        const parts = line.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((p, j) =>
          p.startsWith('**') ? <strong key={j} className="text-ink">{p.slice(2, -2)}</strong>
            : p.startsWith('`') ? <code key={j} className="rounded bg-bg px-1 py-0.5 font-mono text-[12px] ltr">{p.slice(1, -1)}</code> : p)
        return <span key={i}>{parts}{'\n'}</span>
      })}
    </>
  )
}
