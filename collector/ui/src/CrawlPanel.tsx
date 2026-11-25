import { useEffect, useState } from 'react'
import { Alert, Badge, Button, Card, Collapse, Flex, Input, Segmented, Select, Space, Table, Tabs, Tag, Tooltip } from 'antd'
import type { TableColumnsType } from 'antd'
import { ApartmentOutlined, CloudUploadOutlined, DownloadOutlined, FileTextOutlined, PlayCircleOutlined, StopOutlined, UnorderedListOutlined } from '@ant-design/icons'
import TrialPanel from './TrialPanel'
import { collectionName, collectionStatus, taskIssue, taskStage } from './taskMessages'

type Result = { input: string; status: string; message: string }
type RegisteredRun = { domain: string; saved: number }
type Subscription = RegisteredRun & { base_url?: string; needs_config: boolean }
type Job = {
  id: string; url: string; status: string; stage: string; saved: number; scope?: string
  action?: 'verify' | 'backfill' | 'register'; kinds?: string[]
  source_job?: string; can_register?: boolean
  selection?: { label: string; kinds: string[] }; error?: string; warnings?: string[]; created: number; log_url: string; channel?: string
  results?: Record<string, { status?: string; saved?: number; pending?: number; error?: string; reasons?: Record<string, number> }>
  files: { name: string; url: string }[]
}
const labels: Record<string, string> = {
  queued: '排队中', running: '采集中', finished: '本轮完成', partial: '部分完成', failed: '失败', interrupted: '已中断',
}
const registrationHelp = '长期跟踪这个网址，已采内容去重合并。'
const jobTabs = [
  { key: 'crawl', label: '网页采集' },
  { key: 'backfill', label: '站点补采' },
] as const
type JobTab = (typeof jobTabs)[number]['key']
type PanelTab = JobTab | 'active' | 'subscriptions'
const statusFilters = [
  { value: 'all', label: '全部状态' },
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'needsAttention', label: '需处理' },
  { value: 'finished', label: '已完成' },
] as const
type StatusFilter = (typeof statusFilters)[number]['value']
const needsAttention = (job: Job) => ['partial', 'failed', 'interrupted'].includes(job.status)
const queueRank = (job: Job) => job.status === 'running' ? 0 : job.status === 'queued' ? 1 : 2

function jobErrorDetails(job: Job) {
  const errors = new Map<string, string[]>()
  const perCollection: string[] = []
  for (const [kind, result] of Object.entries(job.results ?? {})) {
    if (!result.error) continue
    perCollection.push(result.error)
    errors.set(result.error, [...(errors.get(result.error) ?? []), collectionName(kind)])
  }
  if (job.error && job.error !== perCollection.join('；') && job.error !== [...errors.keys()].join('；') && !errors.has(job.error)) {
    errors.set(job.error, [])
  }
  return errors
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const connectionError = body === undefined || path === '/api/preview' || path === '/api/contract/entry'
    ? '无法连接看板服务，请稍后重试'
    : '连接中断，请先查看任务队列，再重试'
  const response = await fetch(path, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).catch(() => {
    throw new Error(connectionError)
  })
  if (response.status === 401) throw new Error('登录已失效，请刷新页面重新登录')
  const data = await response.json().catch(() => null) as (T & { error?: string }) | null
  if (!response.ok) throw new Error(data?.error || `请求失败 (${response.status})`)
  if (data === null) throw new Error(connectionError)
  return data
}
export default function CrawlPanel({ registeredRuns, subscriptions }: {
  registeredRuns: RegisteredRun[]
  subscriptions?: Subscription[]
}) {
  const [mode, setMode] = useState<'preview' | 'batch'>('preview')
  const [input, setInput] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [results, setResults] = useState<Result[]>([])
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [pending, setPending] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [jobTab, setJobTab] = useState<PanelTab>('active')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [page, setPage] = useState(1)
  const urls = input.split(/\r?\n/).map(x => x.trim()).filter(Boolean)
  const refresh = async () => setJobs((await api<{ jobs: Job[] }>('/api/jobs')).jobs)
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const data = await api<{ jobs: Job[] }>('/api/jobs')
        if (!stopped) { setJobs(data.jobs); setPollError('') }
      } catch (e) { if (!stopped) setPollError((e as Error).message) }
      if (!stopped) timer = setTimeout(poll, 3000)
    }
    poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [])
  function showJobs(tab: PanelTab) {
    setJobTab(tab)
    setStatusFilter('all')
    setPage(1)
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setPending(true); setError('')
    try {
      const data = await api<{ results: Result[] }>('/api/crawl', { urls })
      setResults(data.results)
      setInput(data.results.filter((r: Result) => r.status === 'invalid').map((r: Result) => r.input).join('\n'))
      if (data.results.some((result: Result) => result.status !== 'invalid')) showJobs('crawl')
      await refresh()
    } catch (e) { setError((e as Error).message) }
    finally { setPending(false) }
  }
  async function action(id: string, name: string) {
    setBusy(id)
    try {
      await api(`/api/jobs/${id}/${name}`, {})
      if (name === 'resume' && statusFilter !== 'all') {
        setStatusFilter('all')
        setPage(1)
      }
      await refresh()
      if (name === 'register') showJobs('subscriptions')
    }
    catch (e) { setError((e as Error).message) }
    finally { setBusy(null) }
  }
  async function enqueue(previewId: string) {
    const data = await api<{ results: Result[] }>('/api/crawl', { preview_id: previewId })
    const result: Result = data.results[0]
    if (result.status === 'invalid') throw new Error(result.message)
    showJobs('crawl')
    void refresh().catch((e: Error) => setPollError(e.message))
    return result.message
  }
  const colors: Record<string, string> = { queued: 'default', running: 'processing', finished: 'success', partial: 'warning', failed: 'error', interrupted: 'gold' }
  const registrationJobs = jobs.filter(job => job.action === 'register')
  const registrations = new Map(registrationJobs.map(job => [job.source_job, job.status]))
  const groups = {
    crawl: jobs.filter(job => !job.action),
    backfill: jobs.filter(job => job.action === 'backfill'),
  }
  const webRunning = [...groups.crawl, ...groups.backfill].filter(job => job.status === 'running')
  const activeCount = registeredRuns.length + webRunning.length
  const subscriptionColumns: TableColumnsType<Subscription> = [
    { title: '网址', key: 'url', render: (_, site) => <div className="site-name">
      {site.base_url ? <a href={site.base_url} target="_blank" rel="noreferrer">{site.base_url}</a> : <strong>{site.domain}</strong>}
    </div> },
    { title: '状态', key: 'status', width: 112, render: (_, site) => {
      if (registeredRuns.some(run => run.domain === site.domain)) return <Badge status="processing" text="采集中" />
      return site.needs_config ? <Badge status="warning" text="待配置" /> : <Badge status="default" text="已订阅" />
    } },
    { title: '累计文章', dataIndex: 'saved', width: 110, align: 'right', render: saved => saved.toLocaleString('zh-CN') },
  ]
  const columns: TableColumnsType<Job> = [
    { title: '站点', dataIndex: 'url', width: 310, render: (url, job) => <div className="site-name">
      <strong>{url}</strong><small>{jobTab === 'active'
        ? (job.action === 'backfill' ? '站点补采' : '一次性采集') : new Date(job.created * 1000).toLocaleString('zh-CN')}</small>
    </div> },
    { title: '状态', dataIndex: 'status', width: 112, render: (state, job) =>
      <Tag color={colors[state]}>{job.action === 'register' && state === 'running' ? '合并中' :
        job.action === 'register' && state === 'finished' ? '已加入' : labels[state] || '状态未知'}</Tag> },
    { title: '当前步骤', key: 'stage', render: (_, job) => <div className="site-name">
      <span>{taskIssue(job.error, job.action === 'backfill' ? '补采未完成' :
        job.action === 'register' ? '加入订阅失败' : '采集未完成')
        || taskStage(job.action === 'register' ? job.stage.replace('长期采集', '订阅') : job.stage)}</span>
      {job.channel && <small>{job.channel}</small>}
    </div> },
    { title: '已保存', key: 'saved', dataIndex: 'saved', width: 96, align: 'right', render: saved => saved.toLocaleString('zh-CN') },
    { title: '操作', key: 'actions', width: 210, render: (_, job) => {
      const active = ['queued', 'running'].includes(job.status)
      const registration = registrations.get(job.id)
      const registered = registration === 'finished'
      const registering = registration === 'queued' || registration === 'running'
      return <Space size="small" wrap>
        {job.can_register && !registered && !registering && <Tooltip title={registrationHelp} trigger={['hover', 'focus']}>
          <Button size="small" loading={busy === job.id} onClick={() => void action(job.id, 'register')}>加入订阅</Button>
        </Tooltip>}
        {registered && <small>已订阅</small>}
        {registering && <small>正在加入订阅</small>}
        {job.status !== 'finished' && !registered && !registering && <Button size="small" loading={busy === job.id}
          icon={active ? <StopOutlined /> : <PlayCircleOutlined />}
          onClick={() => void action(job.id, active ? 'stop' : 'resume')}>{active ? '停止' :
            job.action === 'register' ? '重试订阅' : '继续采集'}</Button>}
        {job.status !== 'queued' && <Button size="small" type="link" href={job.log_url}
          target="_blank" rel="noreferrer" icon={<FileTextOutlined />}>日志</Button>}
      </Space>
    } },
  ]
  function jobTable(rows: Job[], emptyText: string, paginated: boolean) {
    return <Table<Job> rowKey="id" size="middle"
          columns={columns} dataSource={rows} scroll={{ x: 930 }}
          pagination={paginated ? { current: page, onChange: setPage, pageSize: 10, hideOnSinglePage: true, showSizeChanger: false } : false}
          locale={{ emptyText: pollError ? '任务状态暂不可用' : emptyText }}
          expandable={{
            rowExpandable: job => !!(job.scope || job.selection || job.results || job.error || job.warnings?.length || job.files.length),
            expandedRowRender: job => {
              const errors = jobErrorDetails(job)
              return <div className="job-detail">
                {job.selection && <p><strong>范围设置</strong> {job.selection.label}
                  {!!job.selection.kinds.length && ` · ${job.selection.kinds.map(collectionName).join('、')}`}</p>}
                {job.scope && <p><strong>采集范围</strong> {job.scope}</p>}
                {!!job.warnings?.length && <Alert type="warning" showIcon title={job.warnings.join('；')} />}
                {job.results && <ul>{Object.entries(job.results).map(([kind, result]) => <li key={kind}>
                  <strong>{collectionName(kind)}</strong> · {collectionStatus(result.status)} · 本次新增 {result.saved || 0} 条
                  {!!result.pending && ` · 接续 ${result.pending} 条待采链接`}
                  {result.reasons && <ul>{Object.entries(result.reasons).map(([reason, count]) =>
                    <li key={reason}>{reason} · {count} 条</li>)}</ul>}
                </li>)}</ul>}
                {errors.size > 0 && <>
                  <strong>原始错误</strong>
                  <ul>{[...errors].map(([error, kinds]) => <li key={error}>
                    {kinds.length > 0 && <strong>{kinds.join('、')} </strong>}<code>{error}</code>
                  </li>)}</ul>
                </>}
                <Space wrap>{job.files.map(file => <Button key={file.name} size="small" href={file.url} download icon={<DownloadOutlined />}>
                  下载 {file.name}
                </Button>)}</Space>
              </div>
            },
          }} />
  }
  const tabJobs = jobTab === 'active' || jobTab === 'subscriptions' ? [] : groups[jobTab].filter(job =>
    statusFilter === 'all' || (statusFilter === 'needsAttention' ? needsAttention(job) : job.status === statusFilter)
  ).sort((a, b) => queueRank(a) - queueRank(b))

  return <>
    <section id="workbench" aria-labelledby="crawl-heading" className="console-section">
      <Card className="panel-card workbench-card" title={<h2 id="crawl-heading"><ApartmentOutlined /> 采集工作台</h2>}
        extra={<Segmented aria-label="任务方式" value={mode} onChange={setMode}
          options={[{ value: 'preview', label: '试采预览' }, { value: 'batch', label: '批量入队' }]} />}>
        <div hidden={mode !== 'preview'}><TrialPanel request={api} enqueue={enqueue} /></div>
        <div hidden={mode !== 'batch'} className="batch-panel">
          <form onSubmit={submit} className="batch-form">
            <label htmlFor="domains">网站域名或网址</label>
            <Input.TextArea id="domains" rows={4} value={input} onChange={e => setInput(e.target.value)}
              placeholder={'example.com\nhttps://another-site.org'} spellCheck={false} required aria-describedby="crawl-help" />
            <div className="batch-actions">
              <span id="crawl-help">每行一个网址，同域名自动合并。</span>
              <Button htmlType="submit" type="primary" icon={<CloudUploadOutlined />} disabled={!urls.length} loading={pending}>
                {urls.length > 1 ? `加入 ${urls.length} 个网站` : '开始采集'}
              </Button>
            </div>
          </form>
          {!!results.length && <Collapse defaultActiveKey={['receipts']} items={[{ key: 'receipts',
            label: `提交结果 · ${results.filter(x => x.status === 'queued').length} 个已入队`,
            children: <ul className="batch-results">{results.map((result, i) => <li key={i}>
              <Tag color={result.status === 'invalid' ? 'error' : result.status === 'queued' ? 'success' : 'default'}>{result.message}</Tag>
              <span>{result.input}</span>
            </li>)}</ul>,
          }]} />}
        </div>
      </Card>
    </section>
    <section id="jobs" aria-labelledby="jobs-heading" className="console-section">
      <Card className="panel-card jobs-card" title={<h2 id="jobs-heading"><UnorderedListOutlined /> 采集任务</h2>}>
        {error && <Alert type="error" showIcon title={error} />}
        {pollError && <Alert type="warning" showIcon title={pollError} />}
        <Tabs activeKey={jobTab} onChange={key => showJobs(key as PanelTab)} destroyOnHidden items={[
          { key: 'active', label: `正在采集 (${activeCount})`, children: jobTab === 'active' && <>
            {registeredRuns.length > 0 && <section aria-labelledby="registered-runs-heading" className="registered-runs">
              <h3 id="registered-runs-heading">
                <span>订阅站点</span>
              </h3>
              <ul>{registeredRuns.map(run => <li key={run.domain}>
                <strong>{run.domain}</strong>
                <Badge status="processing" text="采集中" />
                <span>累计文章 {run.saved.toLocaleString('zh-CN')}</span>
              </li>)}</ul>
            </section>}
            {(webRunning.length > 0 || !registeredRuns.length) && <>
              {registeredRuns.length > 0 && <h3 className="job-group-heading">网页队列</h3>}
              {jobTable(webRunning, '当前没有正在采集的任务', true)}
            </>}
          </> },
          ...jobTabs.map(({ key, label }) => {
            const title = `${label} (${groups[key].length})`
            return {
              key,
              label: title,
              children: jobTab === key && <>
                <Flex justify="end" className="job-toolbar">
                  <Select aria-label="任务状态" size="small" value={statusFilter} options={[...statusFilters]}
                    onChange={value => { setStatusFilter(value); setPage(1) }} />
                </Flex>
                {jobTable(tabJobs, `暂无符合条件的${label}任务`, true)}
              </>,
            }
          }),
          { key: 'subscriptions', label: <Tooltip title="长期跟踪的网址" trigger={['hover', 'focus']}>
            <span>{subscriptions ? `订阅列表 (${subscriptions.length})` : '订阅列表'}</span>
          </Tooltip>, children: jobTab === 'subscriptions' && <Flex vertical gap={12}>
            <Table<Subscription> rowKey="domain" size="middle" columns={subscriptionColumns} dataSource={subscriptions}
              loading={!subscriptions} pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
              locale={{ emptyText: '暂无订阅网址' }} />
            {!!registrationJobs.length && <Collapse size="small"
              defaultActiveKey={registrationJobs.some(job => job.status !== 'finished') ? ['records'] : []}
              items={[{ key: 'records', label: '订阅记录',
                children: jobTable([...registrationJobs].sort((a, b) => queueRank(a) - queueRank(b)), '暂无订阅记录', true),
              }]} />}
          </Flex> },
        ]} />
      </Card>
    </section>
  </>
}
