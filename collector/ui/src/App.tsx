import { useEffect, useMemo, useState } from 'react'
import CrawlPanel from './CrawlPanel'

type KindState = { n: number; page: number; pages: number | null; done: boolean; updated: string; status?: string; last_reason?: string }
type Site = {
  domain: string
  saved: number
  channel?: string
  page: number
  pages: number | null
  posts: number | null
  done: boolean
  updated: string
  mb: number
  pages_n: number
  media_n: number
  kinds: { posts: KindState; pages: KindState; media: KindState }
  needs_config: boolean
  disabled: boolean
  disabled_reason: string
  verified: boolean
  coverage: number | null
  coverage_verdict: string
  coverage_checked_at?: string
  coverage_stale?: boolean
  coverage_detail?: string
  coverage_missing?: number | null
}
type Status = {
  generated: string
  running: boolean
  queue_running: boolean
  tasks_running: number
  concurrency: number
  running_domains: string[]
  blocked: string[]
  total: number
  rate: number
  sites: Site[]
}

const nf = new Intl.NumberFormat('zh-CN')
const PAGE_SIZE = 10

function useStream() {
  const [data, setData] = useState<Status | null>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    const source = new EventSource('/api/stream')
    source.onmessage = (event) => {
      try {
        setData(JSON.parse(event.data))
        setError(false)
      } catch {
        setError(true)
      }
    }
    source.onerror = () => setError(true)
    return () => source.close()
  }, [])
  return { data, error }
}

function useConfig() {
  const [saved, setSaved] = useState(4)
  const [draft, setDraft] = useState(4)
  const [pending, setPending] = useState(false)
  useEffect(() => {
    fetch('/api/config')
      .then((r) => r.json())
      .then((c) => {
        setSaved(c.concurrency)
        setDraft(c.concurrency)
      })
      .catch(() => {})
  }, [])
  const apply = async (value: number) => {
    const n = Math.max(1, Math.trunc(value))
    setPending(true)
    try {
      const c = await (await fetch('/api/concurrency', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ n }),
      })).json()
      setSaved(c.concurrency)
      setDraft(c.concurrency)
    } finally {
      setPending(false)
    }
  }
  return { saved, draft, setDraft, apply, pending }
}

type Health = 'running' | 'blocked' | 'incomplete' | 'done' | 'config' | 'disabled' | 'idle'

function healthOf(site: Site, running: string[], blocked: string[]): Health {
  if (running.includes(site.domain)) return 'running'
  if (blocked.includes(site.domain)) return 'blocked'
  if (site.disabled) return 'disabled'
  if (site.needs_config && site.saved === 0) return 'config'
  const kinds = [site.kinds.posts, site.kinds.pages, site.kinds.media]
  if (kinds.some((k) => !k.done && (['partial', 'interrupted', 'paused', 'blocked'].includes(k.status || '') || (k.pages && k.page <= k.pages)))) return 'incomplete'
  if (site.done) return 'done'
  return 'idle'
}

const HEALTH_LABEL: Record<Health, string> = {
  running: '运行中',
  blocked: '有阻碍',
  incomplete: '不完整',
  done: '已结束',
  config: '待配置',
  disabled: '已禁用',
  idle: '待采',
}

function formatMb(mb: number) {
  if (!mb) return ''
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(1)} MB`
}

function formatTime(iso: string) {
  if (!iso) return ''
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : `${iso}Z`)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString('zh-CN', { hour12: false })
}

function Figures({ data }: { data: Status | null }) {
  return (
    <dl className="figures">
      <div className="figure">
        <dt>已入库文章</dt>
        <dd>{data ? nf.format(data.total) : '暂无'}</dd>
      </div>
      <div className="figure">
        <dt>站点</dt>
        <dd>
          {data ? data.sites.length : '暂无'}
          {data ? <small>{data.tasks_running} 个任务在跑</small> : null}
        </dd>
      </div>
      <div className="figure">
        <dt>当前速率</dt>
        <dd>{data && data.rate > 0 ? nf.format(Math.round(data.rate)) : ''}<small>篇/秒</small></dd>
      </div>
      <div className="figure">
        <dt>队列并发</dt>
        <dd>{data ? data.concurrency : '0'}<small>路</small></dd>
      </div>
    </dl>
  )
}

function Badge({ health, title }: { health: Health; title?: string }) {
  return <span className="status" data-kind={health} title={title}>{HEALTH_LABEL[health]}</span>
}

// 覆盖率核对与"任务结束"是两件事：cursor done 只说明翻页结束，覆盖率核对看外部总量。
function CoverageBadge({ site }: { site: Site }) {
  const verdict = site.coverage_verdict || '未核对'
  const pct = site.coverage != null ? ` ${(site.coverage * 100).toFixed(1)}%` : ''
  const label = site.coverage_stale ? '待刷新' : ({
    '已核对': `已核对${pct}`, '不可验证': '无法核对', '覆盖率不足': `有缺口${pct}`,
    '核对失败': '核对失败', '口径异常': '口径异常',
  }[verdict] || '未核对')
  const time = site.coverage_checked_at ? new Date(site.coverage_checked_at).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }) : ''
  return <div className="coverage-result" title={site.coverage_detail}>
    <span className="coverage" data-kind={site.coverage_stale ? 'warn' : site.verified ? 'ok' : 'warn'}>{label}</span>
    {time && <small>{site.coverage_stale ? '上次 ' : '核对 '}{time}</small>}
    {!site.coverage_stale && site.coverage_missing != null && site.coverage_missing > 0 && <small>数量差额 {site.coverage_missing.toLocaleString()} 条</small>}
    {site.coverage_detail && <small className="coverage-detail">{site.coverage_detail}</small>}
  </div>
}

function SiteTable({ rows, running, blocked, compact = false }: {
  rows: Site[]; running: string[]; blocked: string[]; compact?: boolean
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">站点</th>
            <th scope="col">进度</th>
            <th scope="col" className="num">页</th>
            <th scope="col" className="num">文章</th>
            <th scope="col" className="num">页面 / 媒体</th>
            <th scope="col" className="num">大小</th>
            <th scope="col">状态</th>
            <th scope="col">覆盖率</th>
            {compact ? null : <th scope="col" className="num">更新</th>}
            <th scope="col" aria-label="打开 CSV" />
          </tr>
        </thead>
        <tbody>
          {rows.map((site) => {
            const health = healthOf(site, running, blocked)
            const done = site.done
            const pct = done ? 100 : site.channel === 'wp-api' && site.pages ? Math.min(99, Math.round((site.page / site.pages) * 100)) : null
            const csvHref = `/csv/${encodeURIComponent(site.domain)}.csv`
            const open = () => window.open(csvHref, '_blank', 'noopener')
            return (
              <tr key={site.domain} className="row" role="link" tabIndex={0} onClick={open}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open() } }}
                  aria-label={`打开 ${site.domain} 的 CSV`}>
                <td className="domain">{site.domain}</td>
                <td>
                  {pct === null ? <span className="muted" /> : (
                    <span className="progress">
                      <span className="track"><span className="fill" data-live={health === 'running'} style={{ width: `${pct}%` }} /></span>
                      <span className="pct">{pct}%</span>
                    </span>
                  )}
                </td>
                <td className="num muted">{site.pages ? `${nf.format(site.page)} / ${nf.format(site.pages)}` : `${nf.format(site.page)} 页`}</td>
                <td className="num">{nf.format(site.saved)}</td>
                <td className="num muted">{nf.format(site.pages_n)} / {nf.format(site.media_n)}</td>
                <td className="num muted">{formatMb(site.mb)}</td>
                <td><Badge health={health} title={site.disabled ? site.disabled_reason : Object.entries(site.kinds).filter(([, k]) => !k.done && k.status).map(([name, k]) => `${name}: ${k.status}${k.last_reason ? " · " + k.last_reason : ""}`).join("；")} /></td>
                <td><CoverageBadge site={site} /></td>
                {compact ? null : <td className="num muted">{formatTime(site.updated)}</td>}
                <td className="open" aria-hidden="true">打开 CSV</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Pager({ page, pages, onPage }: { page: number; pages: number; onPage: (p: number) => void }) {
  if (pages <= 1) return null
  const nums: (number | '…')[] = []
  for (let i = 1; i <= pages; i++) {
    if (i === 1 || i === pages || Math.abs(i - page) <= 1) nums.push(i)
    else if (nums[nums.length - 1] !== '…') nums.push('…')
  }
  return (
    <nav className="pager" aria-label="分页">
      <button type="button" className="button-quiet" onClick={() => onPage(Math.max(1, page - 1))} disabled={page === 1}>上一页</button>
      {nums.map((n, i) => n === '…' ? (
        <span key={`e${i}`} className="muted">…</span>
      ) : (
        <button key={n} type="button" className="button-page" aria-current={n === page ? 'page' : undefined}
                onClick={() => onPage(n)}>{n}</button>
      ))}
      <button type="button" className="button-quiet" onClick={() => onPage(Math.min(pages, page + 1))} disabled={page === pages}>下一页</button>
    </nav>
  )
}

export default function App() {
  const { data, error } = useStream()
  const { saved, draft, setDraft, apply, pending } = useConfig()
  const [tab, setTab] = useState<'overview' | 'running' | 'done'>('overview')
  const [page, setPage] = useState(1)
  const dirty = draft !== saved

  const sites = data?.sites ?? []
  const running = data?.running_domains ?? []
  const blocked = data?.blocked ?? []

  const active = useMemo(() => sites.filter((s) => running.includes(s.domain)), [sites, running])
  const finished = useMemo(() => sites.filter((s) => !running.includes(s.domain) && (s.saved > 0 || s.done)), [sites, running])
  const counts = useMemo(() => {
    const c: Record<Health, number> = { running: 0, blocked: 0, incomplete: 0, done: 0, config: 0, disabled: 0, idle: 0 }
    for (const s of sites) c[healthOf(s, running, blocked)]++
    return c
  }, [sites, running, blocked])
  // 外部核对口径与"任务结束"分开：verified = coverage>=95% 且有独立上游总量
  const coverageCounts = useMemo(() => {
    let verified = 0, unavailable = 0
    for (const s of sites) {
      if (s.verified) verified++
      if (s.disabled || s.needs_config) unavailable++
    }
    return { verified, unavailable }
  }, [sites])

  const totalPages = Math.max(1, Math.ceil(finished.length / PAGE_SIZE))
  const currentPage = Math.min(page, totalPages)
  const pageRows = finished.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE)

  const switchTab = (next: typeof tab) => { setTab(next); setPage(1) }

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <h1>网站内容采集</h1>
          <span>{data ? `${sites.length} 个站点` : '连接中'}</span>
        </div>
        <div className="topbar-right">
          <span className="state" data-live={data?.queue_running === true} aria-live="polite">
            {data ? (data.queue_running ? '原队列运行中' : data.running ? '原脚本采集中' : '服务已连接') : '连接中'}
          </span>
          <div className="concurrency">
            <label htmlFor="conc">并发</label>
            <div className="stepper">
              <button type="button" aria-label="减少并发" onClick={() => setDraft(Math.max(1, draft - 1))}>−</button>
              <input id="conc" type="number" min={1} value={draft}
                     onChange={(e) => setDraft(Math.max(1, Number(e.target.value) || 1))} />
              <button type="button" aria-label="增加并发" onClick={() => setDraft(draft + 1)}>+</button>
            </div>
            <button type="button" className="button-primary" disabled={!dirty || pending} onClick={() => apply(draft)}>应用</button>
          </div>
        </div>
      </header>

      <nav className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === 'overview'} className="tab" onClick={() => switchTab('overview')}>
          总览
        </button>
        <button role="tab" aria-selected={tab === 'running'} className="tab" onClick={() => switchTab('running')}>
          正在运行<span className="tab-count">{active.length}</span>
        </button>
        <button role="tab" aria-selected={tab === 'done'} className="tab" onClick={() => switchTab('done')}>
          已结束<span className="tab-count">{finished.length}</span>
        </button>
      </nav>

      <main className="main">
        <CrawlPanel />
        {error && !data ? (
          <div className="notice" data-kind="error"><strong>连不上状态流。</strong> 检查 collector/serve.py 是否在跑，浏览器会自动重连。</div>
        ) : tab === 'overview' ? (
          <>
            <Figures data={data} />
            <div className="summary">
              <div className="summary-item"><span className="summary-n">{counts.running}</span><span className="summary-k">运行中</span></div>
              <div className="summary-item"><span className="summary-n">{counts.incomplete}</span><span className="summary-k">不完整</span></div>
              <div className="summary-item"><span className="summary-n">{counts.blocked}</span><span className="summary-k">有阻碍</span></div>
              <div className="summary-item"><span className="summary-n">{counts.done}</span><span className="summary-k">任务结束</span></div>
              <div className="summary-item"><span className="summary-n">{coverageCounts.verified}</span><span className="summary-k">覆盖率已核对</span></div>
              <div className="summary-item"><span className="summary-n">{coverageCounts.unavailable}</span><span className="summary-k">缺入口/不可访问</span></div>
              <div className="summary-item"><span className="summary-n">{counts.idle}</span><span className="summary-k">待采</span></div>
            </div>
            <div className="table-head"><h2>进度最快</h2><p>按文章数排序，前 8 个站点</p></div>
            <SiteTable compact rows={[...sites].sort((a, b) => b.saved - a.saved).slice(0, 8)} running={running} blocked={blocked} />
          </>
        ) : tab === 'running' ? (
          <>
            <div className="table-head"><h2>正在运行</h2><p>{data ? `${active.length} 个站点 · ${data.tasks_running}/${data.concurrency} 个任务` : ''}</p></div>
            {active.length ? <SiteTable rows={active} running={running} blocked={blocked} />
              : <div className="notice">现在没有正在采集的站点，队列可能已空或已停止。</div>}
          </>
        ) : (
          <>
            <div className="table-head">
              <h2>已结束</h2>
              <p>{`共 ${finished.length} 个站点 · 第 ${currentPage}/${totalPages} 页`}</p>
            </div>
            {finished.length ? (
              <>
                <SiteTable rows={pageRows} running={running} blocked={blocked} />
                <Pager page={currentPage} pages={totalPages} onPage={setPage} />
              </>
            ) : <div className="notice">还没有结束的站点。</div>}
          </>
        )}
      </main>

      <footer className="footer">状态自动更新 · 任务结束不代表全站覆盖率已核实</footer>
    </div>
  )
}
