import { useEffect, useState } from 'react'
import { Alert, Button, Card, Collapse, Input, Segmented, Space, Table, Tag } from 'antd'
import type { TableColumnsType } from 'antd'
import { ApartmentOutlined, CloudUploadOutlined, DownloadOutlined, FileTextOutlined, PlayCircleOutlined, StopOutlined, UnorderedListOutlined } from '@ant-design/icons'
import TrialPanel from './TrialPanel'

type Result = { input: string; status: string; message: string }
type Job = {
  id: string; url: string; status: string; stage: string; saved: number; scope?: string
  selection?: { label: string; kinds: string[] }; error?: string; warnings?: string[]; created: number; log_url: string; channel?: string
  results?: Record<string, { status?: string; saved?: number; error?: string; reasons?: Record<string, number> }>
  files: { name: string; url: string }[]
}
const labels: Record<string, string> = {
  queued: '排队中', running: '采集中', finished: '本轮完成', partial: '部分完成', failed: '失败', interrupted: '已中断',
}
async function api(path: string, body?: unknown) {
  const response = await fetch(path, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (response.status === 401) throw new Error('登录已失效，请刷新页面重新登录')
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`)
  return data
}
export default function CrawlPanel() {
  const [mode, setMode] = useState<'preview' | 'batch'>('preview')
  const [input, setInput] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [results, setResults] = useState<Result[]>([])
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [pending, setPending] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const urls = input.split(/\r?\n/).map(x => x.trim()).filter(Boolean)
  const refresh = async () => setJobs((await api('/api/jobs')).jobs)
  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const data = await api('/api/jobs')
        if (!stopped) { setJobs(data.jobs); setPollError('') }
      } catch (e) { if (!stopped) setPollError((e as Error).message) }
      if (!stopped) timer = setTimeout(poll, 3000)
    }
    poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [])
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setPending(true); setError('')
    try {
      const data = await api('/api/crawl', { urls })
      setResults(data.results)
      setInput(data.results.filter((r: Result) => r.status === 'invalid').map((r: Result) => r.input).join('\n'))
      setPage(1)
      await refresh()
    } catch (e) { setError((e as Error).message) }
    finally { setPending(false) }
  }
  async function action(id: string, name: string) {
    setBusy(id)
    try { await api(`/api/jobs/${id}/${name}`, {}); await refresh() }
    catch (e) { setError((e as Error).message) }
    finally { setBusy(null) }
  }
  async function enqueue(previewId: string) {
    const data = await api('/api/crawl', { preview_id: previewId })
    const result: Result = data.results[0]
    if (result.status === 'invalid') throw new Error(result.message)
    setPage(1)
    void refresh().catch((e: Error) => setPollError(e.message))
    return result.message
  }
  const colors: Record<string, string> = { queued: 'default', running: 'processing', finished: 'success', partial: 'warning', failed: 'error', interrupted: 'gold' }
  const columns: TableColumnsType<Job> = [
    { title: '站点', dataIndex: 'url', width: 310, render: (url, job) => <div className="site-name">
      <strong>{url}</strong><small>{new Date(job.created * 1000).toLocaleString('zh-CN')}</small>
    </div> },
    { title: '状态', dataIndex: 'status', width: 112, render: state => <Tag color={colors[state]}>{labels[state] || state}</Tag> },
    { title: '当前步骤', key: 'stage', render: (_, job) => <div className="site-name">
      <span>{job.stage}</span>{job.channel && <small>{job.channel}</small>}
      {job.error && <span className="task-error">{job.error}</span>}
    </div> },
    { title: '已保存', dataIndex: 'saved', width: 96, align: 'right', render: saved => saved.toLocaleString('zh-CN') },
    { title: '操作', key: 'actions', width: 210, render: (_, job) => {
      const active = ['queued', 'running'].includes(job.status)
      return <Space size="small" wrap>
        {job.status !== 'finished' && <Button size="small" loading={busy === job.id}
          icon={active ? <StopOutlined /> : <PlayCircleOutlined />}
          onClick={() => void action(job.id, active ? 'stop' : 'resume')}>{active ? '停止' : '继续采集'}</Button>}
        {job.status !== 'queued' && <Button size="small" type="link" href={job.log_url}
          target="_blank" rel="noreferrer" icon={<FileTextOutlined />}>日志</Button>}
      </Space>
    } },
  ]

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
      <Card className="panel-card" title={<h2 id="jobs-heading"><UnorderedListOutlined /> 网页采集任务</h2>}
        extra={<span className="panel-meta">{jobs.length} 个任务 · 自动刷新</span>}>
        {error && <Alert type="error" showIcon title={error} />}
        {pollError && <Alert type="warning" showIcon title={pollError} />}
        <Table<Job> rowKey="id" size="middle" columns={columns} dataSource={jobs} scroll={{ x: 930 }}
          pagination={{ current: page, onChange: setPage, pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
          locale={{ emptyText: '还没有网页任务。确认试采结果或批量提交后，任务会出现在这里。' }}
          expandable={{
            rowExpandable: job => !!(job.scope || job.selection || job.results || job.warnings?.length || job.files.length),
            expandedRowRender: job => <div className="job-detail">
              {job.selection && <p><strong>范围设置</strong> {job.selection.label}
                {!!job.selection.kinds.length && ` · ${job.selection.kinds.join('、')}`}</p>}
              {job.scope && <p><strong>采集范围</strong> {job.scope}</p>}
              {job.status === 'finished' && <p>已完成本轮发现的范围。全站覆盖率可在站点监控里查看。</p>}
              {!!job.warnings?.length && <Alert type="warning" showIcon title={job.warnings.join('；')} />}
              {job.results && <ul>{Object.entries(job.results).map(([kind, result]) => <li key={kind}>
                <strong>{kind}</strong> · {result.status} · 本次新增 {result.saved || 0} 条 {result.error}
                {result.reasons && <ul>{Object.entries(result.reasons).map(([reason, count]) =>
                  <li key={reason}>{reason} · {count} 条</li>)}</ul>}
              </li>)}</ul>}
              <Space wrap>{job.files.map(file => <Button key={file.name} size="small" href={file.url} download icon={<DownloadOutlined />}>
                下载 {file.name}
              </Button>)}</Space>
            </div>,
          }} />
      </Card>
    </section>
  </>
}
