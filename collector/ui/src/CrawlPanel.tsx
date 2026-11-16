import { useEffect, useState } from 'react'

type Result = { input: string; status: string; message: string }
type Job = {
  id: string; url: string; status: string; stage: string; saved: number; scope?: string
  error?: string; warnings?: string[]; created: number; log_url: string
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
  const [input, setInput] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [results, setResults] = useState<Result[]>([])
  const [error, setError] = useState('')
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
        if (!stopped) { setJobs(data.jobs); setError('') }
      } catch (e) { if (!stopped) setError((e as Error).message) }
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
  const pageCount = Math.max(1, Math.ceil(jobs.length / 10))
  const current = Math.min(page, pageCount)
  return <section className="crawl-panel" aria-labelledby="crawl-heading">
    <div className="crawl-intro"><span className="eyebrow">新建采集任务</span><h2 id="crawl-heading">输入域名，开始采集</h2>
      <p>自动识别站点并遍历可发现的公开内容。不设条数上限，支持批量排队与断点继续。</p></div>
    <form onSubmit={submit} className="crawl-form">
      <label htmlFor="domains">网站域名或网址 <span>每行一个</span></label>
      <textarea id="domains" rows={4} value={input} onChange={e => setInput(e.target.value)}
        placeholder={'example.com\nhttps://another-site.org'} spellCheck={false} required aria-describedby="crawl-help" />
      <div className="crawl-submit"><p id="crawl-help">同域名自动合并。登录内容、附件原文件及部分动态页面可能需要专门适配。</p>
        <button className="button-primary" disabled={pending || !urls.length}>{pending ? '正在提交…' : urls.length > 1 ? `加入 ${urls.length} 个网站` : '开始采集'}</button></div>
    </form>
    {error && <div className="notice" data-kind="error" role="alert">{error}</div>}
    {!!results.length && <details className="submission-results" open><summary>提交结果 · {results.filter(x => x.status === 'queued').length} 个已入队</summary>
      <ul>{results.map((r, i) => <li key={i} data-invalid={r.status === 'invalid'}><strong>{r.input}</strong><span>{r.message}</span></li>)}</ul></details>}
    <div className="table-head"><h2>网页采集任务</h2><p>{jobs.length ? `${jobs.length} 个任务 · 自动刷新` : '提交后，进度和下载文件会出现在这里'}</p></div>
    {!jobs.length ? <div className="job-empty">还没有网页任务。可以先添加一个网站，也可以一次粘贴整份域名清单。</div> :
      <div className="job-list">{jobs.slice((current - 1) * 10, current * 10).map(job => <article className="crawl-job" key={job.id}>
        <div className="job-title"><h3>{job.url}</h3><span className="job-state" data-state={job.status}>{labels[job.status] || job.status}</span></div>
        <div className="job-meta"><span>{job.stage}</span><strong>已保存 {job.saved.toLocaleString()} 条</strong><time>{new Date(job.created * 1000).toLocaleString('zh-CN')}</time></div>
        {job.scope && <p className="job-scope">采集范围：{job.scope}</p>}
        {job.status === 'finished' && <p className="job-scope">已完成当前发现范围；尚未独立核实整个网站的历史总量。</p>}
        {job.error && <p className="job-error">{job.error}</p>}
        {!!job.warnings?.length && <p className="job-scope">{job.warnings.join('；')}</p>}
        {job.results && <details><summary>查看内容集合结果</summary><ul>{Object.entries(job.results).map(([kind, r]) => <li key={kind}>{kind} · {r.status} · 本次新增 {r.saved || 0} 条 {r.error || ''} {r.reasons ? JSON.stringify(r.reasons) : ''}</li>)}</ul></details>}
        <div className="job-actions">
          {['queued', 'running'].includes(job.status) ? <button className="button-quiet" disabled={busy === job.id} onClick={() => action(job.id, 'stop')}>停止</button> : job.status !== 'finished' && <button className="button-quiet" disabled={busy === job.id} onClick={() => action(job.id, 'resume')}>继续采集</button>}
          {job.status !== 'queued' && <a href={job.log_url} target="_blank" rel="noreferrer">查看日志</a>}
          {job.files.map(file => <a className="download-link" key={file.name} href={file.url} download>下载 {file.name}</a>)}
        </div>
      </article>)}</div>}
    {pageCount > 1 && <div className="pager"><button className="button-quiet" disabled={current === 1} onClick={() => setPage(current - 1)}>上一页</button><span>{current} / {pageCount}</span><button className="button-quiet" disabled={current === pageCount} onClick={() => setPage(current + 1)}>下一页</button></div>}
    <div className="legacy-heading"><h2>已有采集数据</h2><p>以下为原有脚本的数据与队列；上方网页任务独立保存。</p></div>
  </section>
}
