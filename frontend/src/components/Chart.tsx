import { useMemo, useRef, useState } from 'react'
import { clock, n1 } from '../fmt'

type Pt = [number, number]

/** Single-series usage chart with a dashed plan-limit reference line and a crosshair tooltip. */
export function UsageChart({ title, points, limit, unit, height = 120 }: {
  title: string; points: Pt[]; limit?: number | null; unit: string; height?: number
}) {
  const ref = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)
  const W = 320, H = height, padL = 6, padR = 6, padT = 14, padB = 18

  const geo = useMemo(() => {
    if (points.length < 2) return null
    const xs = points.map((p) => p[0]), ys = points.map((p) => p[1])
    const x0 = Math.min(...xs), x1 = Math.max(...xs)
    const yMax = Math.max(...ys, limit ?? 0) * 1.12 || 1
    // Time axis stays left→right (like every monitoring tool) even inside the RTL page.
    const sxl = (x: number) => padL + ((x - x0) / (x1 - x0 || 1)) * (W - padL - padR)
    const sy = (y: number) => padT + (1 - y / yMax) * (H - padT - padB)
    const d = points.map((p, i) => `${i ? 'L' : 'M'}${sxl(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join('')
    const area = `${d}L${sxl(x1).toFixed(1)},${H - padB}L${sxl(x0).toFixed(1)},${H - padB}Z`
    return { sx: sxl, sy, d, area, x0, x1, yMax }
  }, [points, limit, H])

  const last = points.at(-1)?.[1]
  const ratio = limit && last != null ? last / limit : null
  const tone = ratio == null ? 'text-ink-2' : ratio >= 0.9 ? 'text-crit' : ratio >= 0.75 ? 'text-warn' : 'text-ink'

  function onMove(e: React.PointerEvent) {
    if (!geo || !ref.current) return
    const r = ref.current.getBoundingClientRect()
    const x = ((e.clientX - r.left) / r.width) * W
    let best = 0, bd = Infinity
    points.forEach((p, i) => { const dd = Math.abs(geo.sx(p[0]) - x); if (dd < bd) { bd = dd; best = i } })
    setHover(best)
  }

  return (
    <figure className="rounded-xl border border-line bg-panel-2 p-3">
      <figcaption className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-xs text-ink-2">{title}</span>
        <span className={`num text-sm font-semibold ${tone}`}>
          {last != null ? `${n1(last)} ${unit}` : '—'}
          {limit ? <span className="text-xs font-normal text-ink-3"> / {n1(limit)} {unit}</span> : null}
        </span>
      </figcaption>
      {!geo ? (
        <div className="grid h-[100px] place-items-center text-xs text-ink-3">هنوز داده‌ای از API لیارا نرسیده</div>
      ) : (
        <div className="relative ltr">
          <svg ref={ref} viewBox={`0 0 ${W} ${H}`} className="w-full touch-none select-none" role="img"
            aria-label={`${title}: آخرین مقدار ${n1(last)} ${unit}`}
            onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
            <line x1={padL} x2={W - padR} y1={H - padB} y2={H - padB} stroke="#2f3b4e" strokeWidth={1} />
            <path d={geo.area} fill="#2dd4bf" opacity={0.08} />
            <path d={geo.d} fill="none" stroke="#2dd4bf" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            {limit ? (
              <g>
                <line x1={padL} x2={W - padR} y1={geo.sy(limit)} y2={geo.sy(limit)} stroke="#f8645a" strokeWidth={1.2} strokeDasharray="4 4" />
                <text x={padL + 2} y={geo.sy(limit) - 4} fontSize={9} fill="#aab6c3">سقف پلن</text>
              </g>
            ) : null}
            <text x={padL} y={H - 4} fontSize={9} fill="#75828f">{clock(geo.x0)}</text>
            <text x={W - padR} y={H - 4} fontSize={9} fill="#75828f" textAnchor="end">{clock(geo.x1)}</text>
            {hover != null && points[hover] ? (
              <g>
                <line x1={geo.sx(points[hover][0])} x2={geo.sx(points[hover][0])} y1={padT} y2={H - padB} stroke="#aab6c3" strokeWidth={1} opacity={0.5} />
                <circle cx={geo.sx(points[hover][0])} cy={geo.sy(points[hover][1])} r={4} fill="#2dd4bf" stroke="#161d27" strokeWidth={2} />
              </g>
            ) : null}
          </svg>
          {hover != null && points[hover] ? (
            <div className="pointer-events-none absolute top-0 rounded-md border border-line-2 bg-panel-3 px-2 py-1 text-[11px] text-ink shadow-card"
              style={{ left: `${Math.min(70, (geo.sx(points[hover][0]) / W) * 100)}%` }}>
              <span className="num">{n1(points[hover][1])} {unit}</span>
              <span className="text-ink-3"> · {clock(points[hover][0])}</span>
            </div>
          ) : null}
        </div>
      )}
    </figure>
  )
}
