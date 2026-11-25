import { useEffect, useState } from 'react'
import { Alert, Badge, Button, Card, InputNumber, Layout, Progress, Space, Statistic, Table, Tabs, Tag, Tooltip } from 'antd'
import type { TableColumnsType } from 'antd'
import { AppstoreOutlined, CloudServerOutlined, DatabaseOutlined, DashboardOutlined, DownloadOutlined, GlobalOutlined, ThunderboltOutlined } from '@ant-design/icons'
import CrawlPanel from './CrawlPanel'
import SiteQuality from './SiteQuality'

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
  const [configError, setConfigError] = useState('')
  useEffect(() => {
    fetch('/api/config')
      .then((r) => { if (!r.ok) throw new Error(); return r.json() })
      .then((c) => {
        setSaved(c.concurrency)
        setDraft(c.concurrency)
      })
      .catch(() => setConfigError('无法读取并发配置'))
  }, [])
  const apply = async (value: number) => {
    const n = Math.max(1, Math.trunc(value))
    setPending(true)
    setConfigError('')
    try {
      const response = await fetch('/api/concurrency', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ n }),
      })
      if (!response.ok) throw new Error()
      const c = await response.json()
      setSaved(c.concurrency)
      setDraft(c.concurrency)
    } catch {
      setConfigError('并发设置未保存，请重试')
    } finally {
      setPending(false)
    }
  }
  return { saved, draft, setDraft, apply, pending, configError }
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

const healthColors: Partial<Record<Health, string>> = {
  running: 'processing', blocked: 'error', incomplete: 'warning', done: 'success', config: 'gold',
}

function Coverage({ site }: { site: Site }) {
  const pct = site.coverage != null ? ` ${(site.coverage * 100).toFixed(1)}%` : ''
  const label = site.coverage_stale ? '待刷新' : ({
    '已核对': `已核对${pct}`, '不可验证': '无法核对', '覆盖率不足': `有缺口${pct}`,
    '核对失败': '核对失败', '口径异常': '口径异常',
  }[site.coverage_verdict] || '未核对')
  const checked = site.coverage_checked_at && new Date(site.coverage_checked_at).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  })
  return <div className="coverage-cell">
    <Tag color={site.coverage_stale ? 'warning' : site.verified ? 'success' : 'default'}>{label}</Tag>
    {checked && <small>{site.coverage_stale ? '上次' : '核对于'} {checked}</small>}
    {!site.coverage_stale && !!site.coverage_missing && site.coverage_missing > 0 &&
      <small>数量差额 {nf.format(site.coverage_missing)} 条</small>}
    {site.coverage_detail && <small>{site.coverage_detail}</small>}
  </div>
}

function SiteTable({ rows, running, blocked, showTime, paginate }: {
  rows: Site[]; running: string[]; blocked: string[]; showTime?: boolean; paginate?: boolean
}) {
  const columns: TableColumnsType<Site> = [
    { title: '站点', key: 'site', width: 220, render: (_, site) => <div className="site-name">
      <strong>{site.domain}</strong>{site.channel && <small>{site.channel}</small>}
    </div> },
    { title: '采集进度', key: 'progress', width: 150, render: (_, site) => {
      const pct = site.done ? 100 : site.channel === 'wp-api' && site.pages ? Math.min(99, Math.round((site.page / site.pages) * 100)) : null
      return <div className="progress-cell">
        <span>{nf.format(site.page)}{site.pages ? ` / ${nf.format(site.pages)}` : ''} 页</span>
        {pct !== null && <Progress percent={pct} size="small" />}
      </div>
    } },
    { title: '文章', dataIndex: 'saved', align: 'right', width: 90, render: n => nf.format(n),
      sorter: (a, b) => a.saved - b.saved },
    { title: '页面 / 媒体', key: 'collections', align: 'right', width: 108, render: (_, s) => `${nf.format(s.pages_n)} / ${nf.format(s.media_n)}` },
    { title: '大小', dataIndex: 'mb', align: 'right', width: 90, render: formatMb },
    { title: '状态', key: 'state', width: 98, render: (_, site) => {
      const health = healthOf(site, running, blocked)
      const reason = site.disabled ? site.disabled_reason : Object.entries(site.kinds)
        .filter(([, kind]) => !kind.done && kind.status)
        .map(([name, kind]) => `${name} · ${kind.status}${kind.last_reason ? ' · ' + kind.last_reason : ''}`).join('；')
      return <Tooltip title={reason}><Tag color={healthColors[health]}>{HEALTH_LABEL[health]}</Tag></Tooltip>
    } },
    { title: '覆盖率核对', key: 'coverage', render: (_, site) => <Coverage site={site} /> },
  ]
  if (showTime) columns.push({ title: '更新', dataIndex: 'updated', width: 93, render: formatTime })
  columns.push({ title: '导出', key: 'export', width: 85, render: (_, site) =>
    <Button type="link" size="small" icon={<DownloadOutlined />} href={`/csv/${encodeURIComponent(site.domain)}.csv`}
      target="_blank" rel="noreferrer" aria-label={`打开 ${site.domain} 的 CSV`}>CSV</Button>,
  })
  return <Table rowKey="domain" size="middle" columns={columns} dataSource={rows} scroll={{ x: showTime ? 1060 : 980 }}
    pagination={paginate ? { pageSize: PAGE_SIZE, hideOnSinglePage: true, showSizeChanger: false } : false}
    locale={{ emptyText: '当前没有符合条件的站点' }} />
}

function Overview({ data, error }: { data: Status | null; error: boolean }) {
  const state = error ? '连接中断' : data?.queue_running ? '原队列运行中' : data?.running ? '原脚本采集中' : data ? '服务已连接' : '连接中'
  return <section className="overview" aria-labelledby="overview-title">
    <div className="overview-heading">
      <div><h1 id="overview-title">网站内容采集</h1><p>试采校准、批量调度与信源覆盖监控</p></div>
      <div className="live-state"><Badge status={error ? 'error' : data ? 'success' : 'processing'} /><span>{state}</span></div>
    </div>
    <div className="overview-metrics">
      <Statistic title="已登记文章 URL" prefix={<DatabaseOutlined />} value={data?.total ?? '—'} />
      <Statistic title="监控站点" prefix={<GlobalOutlined />} value={data ? data.sites.length : '—'} />
      <Statistic title="当前采集速率" prefix={<ThunderboltOutlined />} value={data ? Math.round(data.rate || 0) : '—'} suffix="篇/秒" />
      <Statistic title="运行任务 / 队列并发" prefix={<CloudServerOutlined />}
        value={data ? `${nf.format(data.tasks_running)} / ${nf.format(data.concurrency)}` : '—'} />
    </div>
  </section>
}

export default function App() {
  const { data, error } = useStream()
  const { saved, draft, setDraft, apply, pending, configError } = useConfig()
  const sites = data?.sites ?? []
  const running = data?.running_domains ?? []
  const blocked = data?.blocked ?? []
  const active = sites.filter(site => running.includes(site.domain))
  const finished = sites.filter(site => !running.includes(site.domain) && (site.saved > 0 || site.done))
  const counts = { running: 0, blocked: 0, incomplete: 0, done: 0, config: 0, disabled: 0, idle: 0 }
  for (const site of sites) counts[healthOf(site, running, blocked)]++
  const tableProps = { running, blocked }
  const signals = [
    { label: '运行中', count: counts.running, color: 'processing' },
    { label: '不完整', count: counts.incomplete, color: 'warning' },
    { label: '有阻碍', count: counts.blocked, color: 'error' },
    { label: '任务结束', count: counts.done, color: 'default' },
    { label: '覆盖率已核对', count: sites.filter(site => site.verified).length, color: 'success' },
    { label: '缺入口或已停用', count: sites.filter(site => site.disabled || site.needs_config).length, color: 'gold' },
    { label: '待采', count: counts.idle, color: 'default' },
  ]

  return <Layout className="console">
    <header className="console-header">
      <a className="console-brand" href="#top"><span className="brand-mark"><AppstoreOutlined /></span><strong>采集控制台</strong></a>
      <nav className="console-nav" aria-label="工作区">
        <a href="#workbench">采集工作台</a><a href="#jobs">任务队列</a><a href="#monitor">站点监控</a>
      </nav>
      <Space className="queue-settings">
        <label htmlFor="conc">队列并发</label>
        <InputNumber id="conc" min={1} precision={0} size="small" value={draft}
          onChange={value => { if (value !== null) setDraft(value) }} />
        <Button size="small" aria-label="保存并发" disabled={draft === saved} loading={pending} onClick={() => void apply(draft)}>应用</Button>
      </Space>
    </header>
    <Layout.Content className="console-main" id="top">
      <Overview data={data} error={error} />
      {configError && <Alert type="error" showIcon title={configError} />}
      <CrawlPanel />
      <section id="monitor" aria-labelledby="monitor-title" className="console-section">
        <Card className="panel-card" title={<h2 id="monitor-title"><DashboardOutlined /> 站点监控</h2>}
          extra={<span className="panel-meta">原有脚本与站点覆盖</span>}>
          {error && <Alert type="warning" showIcon title="状态流连接中断，浏览器正在重连" />}
          <div className="monitor-signals">{signals.map(signal =>
            <Tag key={signal.label} color={signal.color}>{signal.label} <strong>{signal.count}</strong></Tag>,
          )}</div>
          <Tabs items={[
            { key: 'overview', label: '总览', children: <>
              <p className="panel-caption">按已采文章数查看前 8 个站点</p>
              <SiteTable {...tableProps} rows={[...sites].sort((a, b) => b.saved - a.saved).slice(0, 8)} />
            </> },
            { key: 'quality', label: '近 7 天质量', children: <SiteQuality /> },
            { key: 'running', label: `正在运行 (${active.length})`, children: <SiteTable {...tableProps} rows={active} showTime /> },
            { key: 'done', label: `已结束 (${finished.length})`, children: <SiteTable {...tableProps} rows={finished} showTime paginate /> },
          ]} />
        </Card>
      </section>
      <footer className="console-footer">任务状态自动更新。全站覆盖率以独立核对结果为准。</footer>
    </Layout.Content>
  </Layout>
}
