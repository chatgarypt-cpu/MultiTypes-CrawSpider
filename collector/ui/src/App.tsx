import { useEffect, useState } from 'react'
import { Alert, Badge, Button, Card, Checkbox, Collapse, Flex, Form, InputNumber, Layout, Modal, Progress, Radio, Space, Statistic, Table, Tabs, Tooltip, Typography, message } from 'antd'
import type { BadgeProps, TableColumnsType, TableProps } from 'antd'
import { AppstoreOutlined, CloudServerOutlined, DatabaseOutlined, DashboardOutlined, DownloadOutlined, GlobalOutlined, ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import CrawlPanel, { api } from './CrawlPanel'
import SiteQuality from './SiteQuality'
import { collectionName, collectionStatus, taskIssue, taskStage } from './taskMessages'

type KindState = { n: number; page: number; pages: number | null; done: boolean; updated: string; status?: string; last_reason?: string }
type ActionType = 'verify' | 'backfill'
type BackfillMode = 'resume' | 'retry' | 'rescan'
type SiteAction = { id: string; action: ActionType; status: string; stage: string; created: number; requested_at?: number; saved?: number; error?: string }
type SiteActions = Partial<Record<ActionType, SiteAction>>
type ActionTarget = { domain: string; kinds?: string[] }
type ActionResult = { domain: string; job?: SiteAction; duplicate?: boolean; error?: string }
type BackfillDraft = { domains: string[]; kinds: string[]; mode: BackfillMode; batch: boolean }
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
  kinds: Record<string, KindState>
  collections?: string[]
  can_retry_links?: boolean
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
  site_actions?: Record<string, SiteActions>
}

const nf = new Intl.NumberFormat('zh-CN')
const PAGE_SIZE = 10
const SELECTION_WIDTH = 48
const { Text } = Typography

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
  const kinds = Object.values(site.kinds)
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

const healthColors: Partial<Record<Health, BadgeProps['status']>> = {
  running: 'processing', blocked: 'error', incomplete: 'warning', done: 'success', config: 'warning',
}

const activeAction = (action?: SiteAction) => action?.status === 'queued' || action?.status === 'running'
const actionTime = (action: SiteAction) => action.requested_at ?? action.created

function canVerify(site: Site) {
  return !!site.collections?.length && !site.disabled && (site.coverage_stale || !site.verified)
}

function canBackfill(site: Site, health: Health) {
  return !!site.collections?.length && !site.disabled && !site.needs_config &&
    (health === 'incomplete' || health === 'blocked' ||
    (!site.coverage_stale && site.coverage_verdict === '覆盖率不足'))
}

function Coverage({ site, action, disabled, onRefresh }: {
  site: Site; action?: SiteAction; disabled: boolean; onRefresh: () => void
}) {
  const pct = site.coverage != null ? ` ${(site.coverage * 100).toFixed(1)}%` : ''
  const label = site.coverage_stale ? '待刷新' : ({
    '已核对': `已核对${pct}`, '不可验证': '无法核对', '覆盖率不足': `有缺口${pct}`,
    '核对失败': '核对失败', '口径异常': '口径异常',
  }[site.coverage_verdict] || '未核对')
  const checked = site.coverage_checked_at && new Date(site.coverage_checked_at).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  })
  const refreshLabel = site.coverage_checked_at ? '刷新核对' : '开始核对'
  const status = site.coverage_stale || site.coverage_verdict === '覆盖率不足' ? 'warning' :
    site.verified ? 'success' : site.coverage_verdict === '核对失败' ? 'error' : 'default'
  const meta = [
    checked ? `${site.coverage_stale ? '上次' : '核对于'} ${checked}` : '',
    !site.coverage_stale && site.coverage_missing ? `差额 ${nf.format(site.coverage_missing)} 条` : '',
  ].filter(Boolean).join(' · ')
  const issue = activeAction(action) ? '' : taskIssue(action?.error, '核对失败')
  return <div className="coverage-cell">
    <Flex align="center" wrap gap={8} className="monitor-row-actions">
      <Tooltip title={site.coverage_detail} trigger={['hover', 'focus']}>
        <span className="monitor-status" tabIndex={site.coverage_detail ? 0 : undefined}>
          <Badge status={status} text={label} />
        </span>
      </Tooltip>
      {activeAction(action) ? <Text type="secondary" role="status" className="monitor-cell-meta">
        {action?.status === 'queued' ? '核对排队中' : '核对中'}
      </Text> :
        canVerify(site) && <Button color="primary" variant="link" size="small" disabled={disabled}
          aria-label={`${refreshLabel} ${site.domain}`} onClick={onRefresh}>
          {refreshLabel}
        </Button>}
    </Flex>
    {meta && <Text type="secondary" className="monitor-cell-meta">{meta}</Text>}
    {issue && issue !== label && <Text type="secondary" className="monitor-cell-meta">{issue}</Text>}
  </div>
}

function SiteTable({ rows, running, blocked, showTime, paginate, actionFirst, selection, toolbar, actions, submitting, onRefresh, onBackfill }: {
  rows: Site[]; running: string[]; blocked: string[]; showTime?: boolean; paginate?: boolean; actionFirst?: boolean
  selection?: TableProps<Site>['rowSelection']
  toolbar?: TableProps<Site>['title']
  actions: Record<string, SiteActions>; submitting: Set<string>
  onRefresh: (site: Site) => void; onBackfill: (site: Site) => void
}) {
  const columns: TableColumnsType<Site> = [
    { title: '站点', key: 'site', width: actionFirst ? 176 : 220, render: (_, site) => <div className="site-name monitor-site-name">
      <Text strong className="monitor-domain" ellipsis={{ tooltip: site.domain }}>{site.domain}</Text>
      {site.channel && <small>{site.channel}</small>}
    </div> },
    { title: '采集进度', key: 'progress', width: actionFirst ? 110 : 150, render: (_, site) => {
      const pct = site.done ? 100 : site.channel === 'wp-api' && site.pages ? Math.min(99, Math.round((site.page / site.pages) * 100)) : null
      return <div className="progress-cell">
        <span>{nf.format(site.page)}{site.pages ? ` / ${nf.format(site.pages)}` : ''} 页</span>
        {pct !== null && <Progress percent={pct} size="small" strokeColor={site.done ? undefined : 'var(--teal)'} />}
      </div>
    } },
    { title: '文章', dataIndex: 'saved', align: 'right', width: actionFirst ? 76 : 90, render: n => nf.format(n),
      sorter: (a, b) => a.saved - b.saved },
  ]
  if (!actionFirst) columns.push(
    { title: '页面 / 媒体', key: 'collections', align: 'right', width: 108, render: (_, s) => `${nf.format(s.pages_n)} / ${nf.format(s.media_n)}` },
    { title: '大小', dataIndex: 'mb', align: 'right', width: 90, render: formatMb },
  )
  const actionColumns: TableColumnsType<Site> = [
    { title: '采集状态', key: 'state', width: actionFirst ? 120 : 132, render: (_, site) => {
      const health = healthOf(site, running, blocked)
      const reason = site.disabled ? site.disabled_reason : [...new Set(Object.values(site.kinds)
        .filter(kind => !kind.done && kind.last_reason)
        .map(kind => taskIssue(kind.last_reason, collectionStatus(kind.status))))].join('、')
      const activity = actions[site.domain]
      const backfill = activity?.backfill
      const backfillLabel = backfill?.status === 'failed' ? '补采失败' :
        backfill?.status === 'interrupted' ? '补采已中断' : '补采未完成'
      const backfillIssue = taskIssue(backfill?.error, backfillLabel)
      return <div className="site-state">
        <Flex align="center" wrap gap={8} className="monitor-row-actions">
          <Tooltip title={reason} trigger={['hover', 'focus']}>
            <span className="monitor-status" tabIndex={reason ? 0 : undefined}>
              <Badge status={healthColors[health] ?? 'default'} text={HEALTH_LABEL[health]} />
            </span>
          </Tooltip>
          {canBackfill(site, health) && !activeAction(backfill) && <Button color="primary" variant="link" size="small"
            aria-label={`补采 ${site.domain}`}
            disabled={health === 'running' || submitting.has(site.domain) || activeAction(activity?.verify)}
            onClick={() => onBackfill(site)}>补采</Button>}
        </Flex>
        {backfill && <Text role="status" className="monitor-cell-meta" type="secondary">
          {backfill.status === 'queued' ? '补采排队中' : backfill.status === 'running' ? taskStage(backfill.stage) :
            backfillIssue || (backfill.status === 'failed' || backfill.status === 'interrupted' ? backfillLabel :
              `${backfill.status === 'partial' ? '补采部分完成' : '补采完成'} · 新增 ${nf.format(backfill.saved ?? 0)} 条`)}
        </Text>}
      </div>
    } },
    { title: '覆盖率核对', key: 'coverage', render: (_, site) => <Coverage site={site} action={actions[site.domain]?.verify}
      disabled={running.includes(site.domain) || submitting.has(site.domain) || activeAction(actions[site.domain]?.backfill)}
      onRefresh={() => onRefresh(site)} /> },
  ]
  columns.splice(actionFirst ? 1 : columns.length, 0, ...actionColumns)
  if (showTime) columns.push({ title: '更新', dataIndex: 'updated', width: 93, render: formatTime })
  columns.push({ title: '导出', key: 'export', width: actionFirst ? 72 : 85, render: (_, site) =>
    <Button color="primary" variant="link" size="small" icon={<DownloadOutlined />} href={`/csv/${encodeURIComponent(site.domain)}.csv`}
      target="_blank" rel="noreferrer" aria-label={`打开 ${site.domain} 的 CSV`}>CSV</Button>,
  })
  return <Table rowKey="domain" size="small" columns={columns} dataSource={rows} rowSelection={selection} title={toolbar}
    scroll={{ x: (actionFirst ? 730 : showTime ? 1095 : 1015) + (selection ? SELECTION_WIDTH : 0) }}
    pagination={paginate ? { pageSize: PAGE_SIZE, hideOnSinglePage: true, showSizeChanger: false } : false}
    locale={{ emptyText: '当前没有符合条件的站点', selectionMenu: '更多站点选择方式',
      selectionAll: '选择此列表全部可操作站点' }} />
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
  const monitorReady = data !== null
  useEffect(() => {
    if (monitorReady && window.location.hash === '#monitor') document.getElementById('monitor')?.scrollIntoView()
  }, [monitorReady])
  const { saved, draft, setDraft, apply, pending, configError } = useConfig()
  const [notice, noticeHolder] = message.useMessage()
  const [localActions, setLocalActions] = useState<Record<string, SiteActions>>({})
  const [submitting, setSubmitting] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<string[]>([])
  const [batchPending, setBatchPending] = useState<ActionType | null>(null)
  const [batchReport, setBatchReport] = useState<{ summary: string; failures: ActionResult[] } | null>(null)
  const [backfill, setBackfill] = useState<BackfillDraft | null>(null)
  const sites = data?.sites ?? []
  const running = data?.running_domains ?? []
  const blocked = data?.blocked ?? []
  const active = sites.filter(site => running.includes(site.domain))
  const finished = sites.filter(site => !running.includes(site.domain) && (site.saved > 0 || site.done))
  const counts = { running: 0, blocked: 0, incomplete: 0, done: 0, config: 0, disabled: 0, idle: 0 }
  for (const site of sites) counts[healthOf(site, running, blocked)]++
  const actions = { ...data?.site_actions }
  for (const [domain, local] of Object.entries(localActions)) {
    const current = { ...actions[domain] }
    for (const type of ['verify', 'backfill'] as const) {
      const job = local[type]
      const reported = current[type]
      if (job && (!reported || actionTime(reported) < actionTime(job))) current[type] = job
    }
    actions[domain] = current
  }

  const allowsBackfill = (site: Site) => canBackfill(site, healthOf(site, running, blocked))
  function hasActiveWork(site: Site) {
    const tasks = actions[site.domain]
    return running.includes(site.domain) || activeAction(tasks?.verify) || activeAction(tasks?.backfill)
  }
  const actionable = sites.filter(site => !hasActiveWork(site) && (canVerify(site) || allowsBackfill(site)))
  const selectedKeys = new Set(selected)
  const selectedActionable = actionable.filter(site => selectedKeys.has(site.domain) && !submitting.has(site.domain))
  const verifySites = selectedActionable.filter(canVerify)
  const backfillSites = selectedActionable.filter(allowsBackfill)
  const selectableKeys = new Set(actionable.map(site => site.domain))
  const selection: TableProps<Site>['rowSelection'] = {
    columnWidth: SELECTION_WIDTH,
    selections: batchPending ? false : [Table.SELECTION_ALL],
    preserveSelectedRowKeys: true,
    selectedRowKeys: selected,
    onChange: keys => setSelected(keys.map(String)),
    getTitleCheckboxProps: () => ({ 'aria-label': '选择当前页可操作站点' }),
    getCheckboxProps: site => ({
      'aria-label': `选择 ${site.domain}`,
      disabled: !!batchPending || submitting.has(site.domain) || !selectableKeys.has(site.domain),
    }),
  }

  async function submitActions(action: ActionType, targets: ActionTarget[], batch = false, mode?: BackfillMode) {
    const domains = new Set(targets.map(target => target.domain))
    setSubmitting(current => new Set([...current, ...domains]))
    if (batch) {
      setBatchPending(action)
      setBatchReport(null)
    }
    try {
      const { results }: { results: ActionResult[] } = await api('/api/site-actions', { action, targets, mode })
      const accepted = results.filter(result => result.job)
      const failures = results.filter(result => !result.job)
      setLocalActions(current => {
        const next = { ...current }
        for (const { domain, job } of accepted) {
          if (job) next[domain] = { ...next[domain], [job.action]: job }
        }
        return next
      })
      if (batch) {
        const queued = accepted.filter(result => !result.duplicate)
        const duplicates = accepted.length - queued.length
        const completedSelection = new Set(accepted.map(result => result.domain))
        setSelected(current => current.filter(domain => !completedSelection.has(domain)))
        const summary = [
          queued.length > 0 ? `${queued.length} 个站点已加入${action === 'verify' ? '核对' : '补采'}队列` : '',
          duplicates > 0 ? `${duplicates} 个站点已有任务` : '',
          failures.length > 0 ? `${failures.length} 个站点未入队，已保留勾选` : '',
        ].filter(Boolean).join('，')
        setBatchReport({ summary, failures })
        return true
      }
      const result = results[0]
      if (result.error) {
        notice.error(result.error)
        return false
      }
      if (result.duplicate) notice.info('该站点已有任务在排队或运行')
      else notice.success(action === 'verify' ? '覆盖率核对已入队' : '补采已入队')
      return true
    } catch (error) {
      notice.error((error as Error).message)
      return false
    } finally {
      setSubmitting(current => {
        return new Set([...current].filter(domain => !domains.has(domain)))
      })
      if (batch) setBatchPending(null)
    }
  }

  function openBackfill(targets: Site[], batch = false) {
    const incomplete = (site: Site) => site.collections?.filter(kind => !site.kinds[kind]?.done) ?? []
    const kinds = new Set(targets.flatMap(site => {
      const pendingKinds = incomplete(site)
      return pendingKinds.length ? pendingKinds : site.collections ?? []
    }))
    const mode = targets.some(site => incomplete(site).length > 0) ? 'resume' : 'rescan'
    setBackfill({ domains: targets.map(site => site.domain), kinds: [...kinds], mode, batch })
  }

  const draftSites = sites.filter(site => backfill?.domains.includes(site.domain))
  const draftCollectionCounts = new Map<string, number>()
  for (const site of draftSites) {
    for (const kind of site.collections ?? []) {
      draftCollectionCounts.set(kind, (draftCollectionCounts.get(kind) ?? 0) + 1)
    }
  }
  const backfillPlan = draftSites.map(site => {
    const selectedKinds = site.collections?.filter(kind => backfill?.kinds.includes(kind)) ?? []
    const kinds = backfill?.batch && backfill.mode === 'resume'
      ? selectedKinds.filter(kind => !site.kinds[kind]?.done) : selectedKinds
    let reason = ''
    if (hasActiveWork(site) || !allowsBackfill(site)) reason = '站点状态已变化'
    else if (backfill?.mode === 'retry' && !site.can_retry_links) reason = '此站点不支持逐链接重试'
    else if (!selectedKinds.length) reason = '此站点没有所选集合'
    else if (!kinds.length) reason = '所选集合已结束，可重新扫描入口'
    return { domain: site.domain, kinds, reason }
  })
  const backfillTargets = backfillPlan.filter(target => !target.reason).map(({ domain, kinds }) => ({ domain, kinds }))
  const backfillPending = !!backfill?.domains.some(domain => submitting.has(domain))
  const tableProps = { running, blocked, actions, submitting,
    onBackfill: (site: Site) => openBackfill([site]),
    onRefresh: (site: Site) => void submitActions('verify', [{ domain: site.domain }]) }
  const batchToolbar = selected.length ? () => <Flex align="center" justify="space-between" wrap gap={8}
    className="monitor-batch-toolbar" role="group" aria-label="站点批量操作">
    <Text aria-live="polite">已选 <Text strong>{selected.length}</Text> 个站点</Text>
    <Space size={8} wrap>
      <Button size="small" icon={<ReloadOutlined />} disabled={!!batchPending || !verifySites.length}
        loading={batchPending === 'verify'} aria-label={`批量刷新核对 ${verifySites.length} 个站点`}
        onClick={() => void submitActions('verify', verifySites.map(({ domain }) => ({ domain })), true)}>
        批量刷新核对 <span className="monitor-count">{verifySites.length}</span>
      </Button>
      <Button size="small" disabled={!!batchPending || !backfillSites.length}
        aria-label={`批量补采 ${backfillSites.length} 个站点`} onClick={() => openBackfill(backfillSites, true)}>
        批量补采 <span className="monitor-count">{backfillSites.length}</span>
      </Button>
      <Button type="text" size="small" disabled={!!batchPending} onClick={() => setSelected([])}>清除选择</Button>
    </Space>
  </Flex> : undefined
  const signals = [
    { label: '不完整', count: counts.incomplete },
    { label: '有阻碍', count: counts.blocked },
    { label: '采集完成', count: counts.done },
    { label: '覆盖率已核对', count: sites.filter(site => site.verified).length },
    { label: '缺入口或已停用', count: sites.filter(site => site.disabled || site.needs_config).length },
    { label: '待采', count: counts.idle },
  ]

  return <Layout className="console">
    {noticeHolder}
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
        <Card className="panel-card" title={<h2 id="monitor-title"><DashboardOutlined /> 站点监控</h2>}>
          {error && <Alert type="warning" showIcon title="状态流连接中断，浏览器正在重连" />}
          <Space size={[18, 6]} wrap className="monitor-summary">{signals.map(signal =>
            <Text type="secondary" key={signal.label}>{signal.label} <Text strong>{nf.format(signal.count)}</Text></Text>,
          )}</Space>
          {batchReport && <Alert className="monitor-batch-report" showIcon
            type={batchReport.failures.length ? 'warning' : 'success'} title={batchReport.summary}
            closable={{ onClose: () => setBatchReport(null) }}
            description={batchReport.failures.length > 0 && <ul className="monitor-action-details">
              {batchReport.failures.map(({ domain, error }) => <li key={domain}><strong>{domain}</strong><span>{error}</span></li>)}
            </ul>} />}
          <Tabs items={[
            { key: 'overview', label: '总览', children: <SiteTable {...tableProps} selection={selection}
              toolbar={batchToolbar}
              rows={[...sites].sort((a, b) => b.saved - a.saved).slice(0, 8)} /> },
            { key: 'actionable', label: `待处理 (${actionable.length})`, children: <SiteTable {...tableProps}
              selection={selection} toolbar={batchToolbar} rows={actionable} actionFirst paginate /> },
            { key: 'quality', label: '近 7 天质量', children: <SiteQuality /> },
            { key: 'running', label: `正在运行 (${active.length})`, children: <SiteTable {...tableProps} rows={active} showTime /> },
            { key: 'done', label: `已结束 (${finished.length})`, children: <SiteTable {...tableProps}
              selection={selection} toolbar={batchToolbar} rows={finished} showTime paginate /> },
          ]} />
        </Card>
      </section>
      {backfill && <Modal open centered width={480}
        title={backfill.batch ? `批量补采 ${backfill.domains.length} 个站点` : `补采 ${backfill.domains[0]}`}
        okText="加入补采队列" cancelText="取消" closable={!backfillPending} confirmLoading={backfillPending}
        styles={{
          container: { display: 'flex', flexDirection: 'column', maxHeight: 'calc(100dvh - 32px)' },
          body: { minHeight: 0, overflowY: 'auto' },
        }}
        okButtonProps={{ disabled: backfillPending || !backfillTargets.length }} cancelButtonProps={{ disabled: backfillPending }}
        onCancel={() => { if (!backfillPending) setBackfill(null) }}
        onOk={async () => {
          if (await submitActions('backfill', backfillTargets, backfill.batch, backfill.mode)) setBackfill(null)
        }}>
        <Form component="div" layout="vertical" className="backfill-form">
          <Form.Item label={<Text strong id="backfill-collections-label">采集集合</Text>}>
            <Checkbox.Group aria-labelledby="backfill-collections-label" value={backfill.kinds} disabled={backfillPending}
              options={[...draftCollectionCounts].map(([kind, count]) => ({
                label: <>{collectionName(kind)}{backfill.batch && <Text type="secondary" className="monitor-cell-meta"> {count} 站</Text>}</>,
                value: kind,
              }))}
              onChange={kinds => setBackfill(current => current && ({ ...current, kinds }))} />
          </Form.Item>
          <Form.Item label={<Text strong id="backfill-method-label">补采方式</Text>}>
            <Radio.Group aria-labelledby="backfill-method-label" value={backfill.mode} disabled={backfillPending}
              onChange={event => setBackfill(current => current && ({ ...current, mode: event.target.value }))}>
              <Space orientation="vertical" size={12}>
                <Radio value="resume">继续采集
                  <Text type="secondary" className="backfill-method-help">
                    沿用现有断点和队列。{backfill.batch && '可继续采集未完成的集合。'}
                  </Text>
                </Radio>
                {draftSites.some(site => site.can_retry_links) && <Radio value="retry">重试可恢复项
                  <Text type="secondary" className="backfill-method-help">重新处理临时失败或正文提取失败的链接。</Text>
                </Radio>}
                <Radio value="rescan">重新扫描入口
                  <Text type="secondary" className="backfill-method-help">重新发现所选集合，保留并去重已有数据。</Text>
                </Radio>
              </Space>
            </Radio.Group>
          </Form.Item>
        </Form>
        {backfill.batch ? <Collapse ghost size="small"
          key={backfillTargets.length < backfill.domains.length ? 'excluded' : 'all'}
          defaultActiveKey={backfillTargets.length < backfill.domains.length ? ['scope'] : []}
          styles={{ header: { paddingInline: 0 }, body: { paddingInline: 0 } }}
          items={[{
            key: 'scope',
            label: '可补采站点',
            extra: <Text type="secondary">{backfillTargets.length} / {backfill.domains.length} 站</Text>,
            children: <ul className="monitor-action-details">
              {backfillPlan.map(({ domain, kinds, reason }) => <li key={domain}>
                <strong>{domain}</strong><Text type="secondary">{reason || kinds.map(collectionName).join('、')}</Text>
              </li>)}
            </ul>,
          }]} /> : backfillPlan[0]?.reason && <Alert type="warning" showIcon title={backfillPlan[0].reason} />}
      </Modal>}
      <footer className="console-footer">任务状态自动更新。全站覆盖率以独立核对结果为准。</footer>
    </Layout.Content>
  </Layout>
}
