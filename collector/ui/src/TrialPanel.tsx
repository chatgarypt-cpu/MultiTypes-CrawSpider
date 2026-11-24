import { useState } from 'react'
import { Alert, Button, Collapse, Empty, Input, Select, Spin, Steps, Tag } from 'antd'
import { CheckCircleOutlined, ExperimentOutlined, FileSearchOutlined, GlobalOutlined, LinkOutlined, SettingOutlined } from '@ant-design/icons'

const fields = { title: '标题', created_at: '发布日期', text: '正文' } as const
type Field = keyof typeof fields
type Rules = Partial<Record<Field, string>>
type Match = { status: string; matches: number; value: string }
type Checks = Partial<Record<Field, Match>>
type Sample = { kind?: string; mime?: string; url: string; title: string; created_at: string; text: string; text_length: number; fields: Checks }
type Issue = { url: string; reason: string; http_status: number | null; field?: Field; fields?: Checks }
type Option = { value: string; label: string }
type Preview = {
  preview_id: string | null; channel: string; transport: string; source: string
  samples: Sample[]; issues: Issue[]; rules: Rules; editable: boolean
  available_kinds: Option[]; selection?: { label: string }
}
const fieldKeys = Object.keys(fields) as Field[]
const channels: Record<string, string> = { html: '网页列表', rss: 'RSS / Atom', sitemap: '站点地图', 'wp-api': 'WordPress REST', 'json-api': 'JSON API', 'json-list': 'JSON 列表' }
const scopeOptions = [
  { value: 'default', label: '按通道默认范围' },
  { value: 'all', label: '通道可发现的全部内容' },
  { value: 'section', label: '当前栏目（按列表发现）' },
  { value: 'path', label: '指定 URL 路径' },
]
const defaultKinds = [
  { value: 'posts', label: '文章 / 新闻' },
  { value: 'pages', label: '页面' },
  { value: 'media', label: '媒体元数据' },
]
const issueLabels: Record<string, string> = {
  invalid_xpath: 'XPath 语法有误',
  rule_no_sample_match: '这条规则在试采样本中未取到值',
  preview_unavailable: '当前通道或连接方式暂不支持试采，可在批量入队中提交',
  discovery_failed: '无法读取发现入口',
  fetch_failed: '请求失败',
  invalid_api_response: '接口未返回文章列表',
  invalid_sitemap: '响应不是有效的站点地图',
  invalid_selector: '列表规则无法解析',
  no_links: '没有发现文章链接',
  no_article: '未识别到可用正文',
  no_collection_sample: '该类型未取得可用样本',
  auto_wp_no_article: 'WordPress REST 抽样无正文，已尝试网页列表',
  no_body: '响应正文为空',
  access_restricted: '文章标记为访问受限',
  challenge: '站点返回访问验证页面',
  robots: 'robots.txt 禁止访问此路径',
}
const matchLabels: Record<string, string> = {
  invalid_xpath: '规则无效', no_match: '未匹配', empty_value: '匹配值为空', no_document: '页面无法解析',
}

function SourceLink({ url, children }: { url: string; children: string }) {
  try {
    const link = new URL(url)
    if (link.protocol === 'https:' || link.protocol === 'http:') {
      return <a href={link.href} target="_blank" rel="noreferrer">{children}</a>
    }
  } catch { /* Keep malformed publisher URLs as text. */ }
  return <span>{children}</span>
}

function FieldChecks({ checks }: { checks: Checks }) {
  const keys = fieldKeys.filter(field => checks[field])
  if (!keys.length) return null
  return <dl className="trial-checks">
    {keys.map(field => {
      const check = checks[field]!
      return <div key={field}><dt>{fields[field]}规则</dt><dd>
        {check.status === 'matched' ? <><Tag color="success">{check.matches} 处匹配</Tag><span>{check.value.slice(0, 280)}{check.value.length > 280 ? '…' : ''}</span></> :
          <Tag color="warning">{matchLabels[check.status] || check.status}{check.matches ? `（${check.matches} 处）` : ''}</Tag>}
      </dd></div>
    })}
  </dl>
}

type Props = {
  request: (path: string, body: unknown) => Promise<Preview>
  enqueue: (previewId: string) => Promise<string>
}

export default function TrialPanel({ request, enqueue }: Props) {
  const [url, setUrl] = useState('')
  const [channel, setChannel] = useState('auto')
  const [selector, setSelector] = useState('')
  const [scope, setScope] = useState('default')
  const [path, setPath] = useState('')
  const [kinds, setKinds] = useState<string[]>([])
  const [kindOptions, setKindOptions] = useState<Option[]>([])
  const [scopeChanged, setScopeChanged] = useState(false)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [rules, setRules] = useState<Rules>({})
  const [pending, setPending] = useState<'sample' | 'enqueue' | null>(null)
  const [feedback, setFeedback] = useState('')
  const [error, setError] = useState('')
  const dirty = !!preview && fieldKeys.some(field => (rules[field] || '').trim() !== (preview.rules[field] || ''))

  function invalidateScope() {
    setScopeChanged(true)
    setFeedback('')
    setError('')
  }

  async function trial() {
    setPending('sample')
    setFeedback('')
    setError('')
    try {
      const result = await request('/api/preview', { url, rules, channel,
        item_selector: channel === 'html' && scope !== 'all' ? selector : '',
        scope: { mode: scope, path, kinds } })
      setPreview(result)
      setRules(result.rules)
      setKindOptions(result.available_kinds || [])
      setScopeChanged(false)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setPending(null)
    }
  }

  async function confirm() {
    if (!preview?.preview_id || dirty || scopeChanged) return
    setPending('enqueue')
    setError('')
    try {
      setFeedback(await enqueue(preview.preview_id))
      setPreview({ ...preview, preview_id: null })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setPending(null)
    }
  }

  const samples = preview?.samples ?? []
  const issues = preview?.issues ?? []
  const reviewedRules = Object.values(preview?.rules ?? {}).some(Boolean)
  const step = feedback ? 3 : dirty || reviewedRules ? 2 : samples.length ? 1 : 0
  return <>
    <div className="workflow-strip">
      <Steps size="small" responsive={false} current={step} status={feedback ? 'finish' : 'process'}
        items={[
          { title: '试采来源', icon: <GlobalOutlined /> },
          { title: '查看样本', icon: <FileSearchOutlined /> },
          { title: '按需复测', icon: <ExperimentOutlined /> },
          { title: '确认入队', icon: <CheckCircleOutlined /> },
        ]} />
    </div>
    <div className="trial-grid">
      <aside className="trial-sidebar">
        <form onSubmit={event => { event.preventDefault(); void trial() }}>
          <label htmlFor="trial-url">站点或文章网址</label>
          <Input id="trial-url" prefix={<LinkOutlined />} placeholder="example.org/news" autoComplete="url" spellCheck={false}
            value={url} disabled={!!pending} onChange={event => {
              setUrl(event.target.value)
              setPreview(null)
              setRules({})
              setSelector('')
              setKindOptions([])
              setKinds([])
              setError('')
              setFeedback('')
            }} />
          <label htmlFor="trial-channel">采集通道</label>
          <Select id="trial-channel" value={channel} disabled={!!pending} options={[
            { value: 'auto', label: '自动识别' },
            ...['html', 'rss', 'sitemap', 'wp-api'].map(value => ({ value, label: channels[value] })),
          ]} onChange={value => {
            setChannel(value)
            setScope('default')
            setKinds([])
            setKindOptions([])
            setPreview(null)
            setRules({})
            setError('')
            setFeedback('')
          }} />
          <label htmlFor="trial-scope">采集范围</label>
          <Select id="trial-scope" value={scope} disabled={!!pending}
            options={scopeOptions.filter(option => (channel === 'auto' || channel === 'html' || option.value !== 'section')
              && (channel !== 'wp-api' || option.value !== 'path'))}
            onChange={value => {
              setScope(value)
              if (value === 'path' || value === 'section') setKinds([])
              invalidateScope()
            }} />
          {scope === 'path' && <Input aria-label="URL 路径前缀" placeholder="/news" value={path}
            disabled={!!pending} onChange={event => { setPath(event.target.value); invalidateScope() }} />}
          {(channel === 'wp-api' || (channel === 'auto' && kindOptions.length > 0)) && scope !== 'path' && scope !== 'section' && <>
            <label htmlFor="trial-kinds">内容类型</label>
            <Select id="trial-kinds" mode="multiple" value={kinds} allowClear disabled={!!pending}
              placeholder="全部公开类型" options={kindOptions.length ? kindOptions : defaultKinds}
              onChange={values => { setKinds(values); invalidateScope() }} />
            <small>留空采集全部公开类型。文章样本用于核对正文，媒体仅采元数据。</small>
          </>}
          {channel === 'html' && scope !== 'all' && <details>
            <summary>高级选项 · 详情链接规则</summary>
            <Input aria-label="详情链接 XPath" value={selector} placeholder="留空自动查找，应返回 href"
              disabled={!!pending} onChange={event => {
                setSelector(event.target.value)
                setPreview(null)
                setError('')
                setFeedback('')
              }} />
          </details>}
          <Button htmlType="submit" type={samples.length ? 'default' : 'primary'} icon={<ExperimentOutlined />}
            disabled={!url.trim() || !!pending || (scope === 'path' && !path.trim())} loading={pending === 'sample'}>
            {dirty ? '复测字段' : scopeChanged && preview ? '按新范围试采' : '试采样本'}
          </Button>
        </form>
        {preview?.channel && <dl className="trial-source">
          <div><dt>实际通道</dt><dd><Tag color="cyan">{channels[preview.channel] || preview.channel}</Tag></dd></div>
          <div><dt>连接方式</dt><dd>{preview.transport === 'direct' ? 'HTTP 直连' : preview.transport}</dd></div>
          {preview.selection && <div><dt>采集范围</dt><dd>{preview.selection.label}</dd></div>}
          <div><dt>来源</dt><dd><SourceLink url={preview.source}>{preview.source}</SourceLink></dd></div>
        </dl>}
        {preview?.editable && <Collapse size="small" className="field-editor" items={[{
          key: 'rules', label: <span><SettingOutlined /> 字段规则 <Tag>XPath</Tag></span>,
          children: <div className="rule-inputs">
            {fieldKeys.map(field => <div key={field}>
              <label htmlFor={`rule-${field}`}>{fields[field]}</label>
              <Input id={`rule-${field}`} size="small" spellCheck={false} placeholder="自动提取"
                value={rules[field] ?? ''} disabled={!!pending} onChange={event => setRules({ ...rules, [field]: event.target.value })} />
            </div>)}
            <small>修改后复测，查看匹配数量与实际取值。</small>
          </div>,
        }]} />}
      </aside>

      <section className="trial-output" aria-labelledby="trial-results" aria-busy={pending === 'sample'}>
        <header className="trial-output-heading">
          <h3 id="trial-results"><FileSearchOutlined /> 样本预览</h3>
          {preview && <Tag color={samples.length ? 'success' : 'default'}>{samples.length} 条{samples.some(sample => sample.kind === 'media') ? '样本' : '正文'}</Tag>}
        </header>
        <div className="trial-content">
          {error && <Alert type="error" showIcon title={error} />}
          {pending === 'sample' ? <div className="trial-empty" role="status">
            <Spin /><strong>正在检查来源并提取样本</strong>
          </div> : !preview ? <Empty className="trial-empty" image={<FileSearchOutlined />} description={
            <><strong>先看正文，再跑全站</strong><p>输入网址并试采，这里会显示实际提取的内容。</p></>
          } /> : <>
            {!!samples.length && <Collapse defaultActiveKey={['0']} items={samples.map((sample, i) => ({
              key: String(i),
              label: <div className="sample-title">
                <strong>{sample.title || '未提取到标题'}</strong>
                {sample.kind && <Tag>{[...kindOptions, ...defaultKinds].find(item => item.value === sample.kind)?.label || sample.kind}</Tag>}
                <small>{sample.created_at ? `${sample.created_at.slice(0, 10)} · ` : ''}{sample.kind === 'media' ? sample.mime || '媒体元数据' : `${sample.text_length.toLocaleString()} 字符`}</small>
              </div>,
              children: <div className="sample-body">
                <SourceLink url={sample.url}>{sample.kind === 'media' ? '打开媒体链接' : '打开原文'}</SourceLink>
                {sample.text && <p>{sample.text}{sample.text_length > sample.text.length ? '…' : ''}</p>}
                <FieldChecks checks={sample.fields} />
              </div>,
            }))} />}
            {!!issues.length && <Collapse className="trial-diagnostics" defaultActiveKey={samples.length && preview.preview_id ? [] : ['issues']}
              items={[{ key: 'issues', label: <span>请求与提取诊断 <Tag color="warning">{issues.length}</Tag></span>,
                children: <ul>{issues.map((issue, i) => <li key={i}>
                  <strong>{issue.field ? `${fields[issue.field]} · ` : ''}{issueLabels[issue.reason] || issue.reason}
                    {issue.http_status && issue.http_status > 0 && issue.http_status !== 200 ? ` · HTTP ${issue.http_status}` : ''}</strong>
                  <small>{issue.url}</small>
                  {issue.fields && <FieldChecks checks={issue.fields} />}
                </li>)}</ul>,
              }]} />}
          </>}
        </div>
        <footer className="trial-footer">
          <span role="status" className={feedback ? 'trial-success' : ''}>{feedback || (scopeChanged ? '采集范围已修改，请重新试采' : dirty ? '字段规则已修改，请先复测' : '试采样本不会写入采集数据')}</span>
          <Button type="primary" icon={<CheckCircleOutlined />} disabled={!preview?.preview_id || dirty || scopeChanged || !!pending}
            loading={pending === 'enqueue'} onClick={() => void confirm()}>确认加入队列</Button>
        </footer>
      </section>
    </div>
  </>
}

