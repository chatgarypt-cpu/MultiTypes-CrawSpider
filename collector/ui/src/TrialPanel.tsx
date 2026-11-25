import { useState } from 'react'
import { Alert, Button, Collapse, Empty, Input, Select, Spin, Steps, Switch, Tag } from 'antd'
import { CheckCircleOutlined, ExperimentOutlined, FileSearchOutlined, GlobalOutlined, LinkOutlined, SettingOutlined } from '@ant-design/icons'
import EndpointCandidates from './EndpointCandidates'
import type { Candidate, CandidateSource } from './EndpointCandidates'

const fields = { title: '标题', created_at: '发布日期', text: '正文' } as const
type Field = keyof typeof fields
type Rules = Partial<Record<Field, string>>
type Match = { status: string; matches: number; value: string }
type Checks = Partial<Record<Field, Match>>
type Sample = { kind?: string; mime?: string; url: string; title: string; created_at: string; text: string; text_length: number; fields: Checks }
type Issue = { url: string; reason: string; http_status: number | null; field?: Field; fields?: Checks }
type Option = { value: string; label: string }
type Preview = {
  preview_id: string | null; url: string; channel: string; transport: string; source: string
  samples: Sample[]; issues: Issue[]; rules: Rules; editable: boolean
  available_kinds: Option[]; selection?: { label: string }
  candidates?: Candidate[]; capture_note?: CandidateSource
}
const fieldKeys = Object.keys(fields) as Field[]
const channels: Record<string, string> = { html: '网页列表', rss: 'RSS / Atom', sitemap: '站点地图', 'wp-api': 'WordPress REST', 'json-api': 'JSON 接口', 'json-list': 'JSON 列表', archive: '网页存档' }
const isApi = (channel: string) => channel === 'json-api' || channel === 'json-list'
type ApiFields = { path: string; detail_path: string; page_param: string }
const emptyApi: ApiFields = { path: '', detail_path: '', page_param: 'page' }
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
  robots_blocked: 'robots.txt 禁止采集此路径',
  blocked: '站点拒绝访问或限制了请求',
  invalid_api_response: '接口未返回文章列表',
  invalid_api_configuration: '接口配置不完整',
  archive_index_unavailable: '暂时无法读取存档目录',
  archive_miss: '未找到公开存档',
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
  request: <T>(path: string, body: unknown) => Promise<T>
  enqueue: (previewId: string) => Promise<string>
}

export default function TrialPanel({ request, enqueue }: Props) {
  const [url, setUrl] = useState('')
  const [captureContract, setCaptureContract] = useState(true)
  const [channel, setChannel] = useState('auto')
  const [apiFields, setApiFields] = useState<ApiFields>(emptyApi)
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
  const explicitSource = isApi(channel) || channel === 'archive'

  function invalidateScope() {
    setScopeChanged(true)
    setFeedback('')
    setError('')
  }

  function changeChannel(value: string) {
    setChannel(value)
    setScope('default')
    setKinds([])
    setKindOptions([])
    setPreview(null)
    setRules({})
    setApiFields(emptyApi)
    setScopeChanged(false)
    setError('')
    setFeedback('')
  }

  async function trial(nextChannel = channel, nextApi = apiFields) {
    const useApi = isApi(nextChannel)
    const standard = !useApi && nextChannel !== 'archive'
    setPending('sample')
    setFeedback('')
    setError('')
    try {
      const result = await request<Preview>('/api/preview', { url, rules: standard ? rules : {}, channel: nextChannel,
        item_selector: nextChannel === 'html' && scope !== 'all' ? selector : '',
        scope: standard ? { mode: scope, path, kinds } : { mode: 'default' },
        ...(useApi ? { api: nextApi } : {}),
        ...(standard && captureContract ? { capture_contract: true } : {}) })
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

  function trialCandidate(candidate: Candidate) {
    const template = candidate.path_template
    if (!isApi(candidate.channel) || !template) return
    let endpoint = template
    try {
      const observed = new URL(candidate.evidence?.sample_url ?? '')
      endpoint = observed.pathname + observed.search
    } catch { /* The candidate still has a path template. */ }
    const config = {
      path: endpoint,
      detail_path: template.includes('{id}') ? template : template.replace(/\/$/, '') + '/{id}',
      page_param: candidate.pagination?.param ?? 'page',
    }
    changeChannel(candidate.channel)
    setApiFields(config)
    void trial(candidate.channel, config)
  }

  function updateApi(field: keyof ApiFields, value: string) {
    setApiFields({ ...apiFields, [field]: value })
    invalidateScope()
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
              setApiFields(emptyApi)
              setKindOptions([])
              setKinds([])
              setError('')
              setFeedback('')
            }} />
          <label htmlFor="trial-channel">采集通道</label>
          <Select id="trial-channel" value={channel} disabled={!!pending} virtual={false} listHeight={320} options={[
            { value: 'auto', label: '自动识别' },
            ...Object.entries(channels).map(([value, label]) => ({ value, label })),
          ]} onChange={changeChannel} />
          {isApi(channel) && <>
            <label htmlFor="trial-api-path">接口路径</label>
            <Input id="trial-api-path" value={apiFields.path} placeholder="/api/articles" spellCheck={false}
              disabled={!!pending} onChange={event => updateApi('path', event.target.value)} />
          </>}
          {channel === 'json-list' && <>
            <label htmlFor="trial-api-page">分页参数</label>
            <Input id="trial-api-page" value={apiFields.page_param} placeholder="page" spellCheck={false}
              disabled={!!pending} onChange={event => updateApi('page_param', event.target.value)} />
            <label htmlFor="trial-api-detail">详情路径模板</label>
            <Input id="trial-api-detail" value={apiFields.detail_path} placeholder="/news/{id}" spellCheck={false}
              disabled={!!pending} onChange={event => updateApi('detail_path', event.target.value)} />
          </>}
          {!explicitSource && <>
            <label htmlFor="trial-scope">采集范围</label>
            <Select id="trial-scope" value={scope} disabled={!!pending}
              options={scopeOptions.filter(option => (channel === 'auto' || channel === 'html' || option.value !== 'section')
                && (channel !== 'wp-api' || option.value !== 'path'))}
              onChange={value => {
                setScope(value)
                if (value === 'path' || value === 'section') setKinds([])
                invalidateScope()
              }} />
          </>}
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
            disabled={!url.trim() || !!pending || (scope === 'path' && !path.trim()) ||
              (isApi(channel) && !apiFields.path.trim()) ||
              (channel === 'json-list' && (!apiFields.page_param.trim() || !apiFields.detail_path.trim()))}
            loading={pending === 'sample'}>
            {dirty ? '复测字段' : scopeChanged && preview && !explicitSource ? '按新范围试采' : '试采样本'}
          </Button>
        </form>
        {preview?.channel && <dl className="trial-source">
          <div><dt>实际通道</dt><dd><Tag color="cyan">{channels[preview.channel] || preview.channel}</Tag></dd></div>
          <div><dt>连接方式</dt><dd>{preview.transport === 'direct' ? 'HTTP 直连' :
            preview.transport === 'archive' ? '公开存档' : preview.transport}</dd></div>
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
          <div className="trial-output-tools">
            {preview && <Tag color={samples.length ? 'success' : 'default'}>{samples.length} 条{samples.some(sample => sample.kind === 'media') ? '样本' : '正文'}</Tag>}
            {!explicitSource && <label className="trial-capture-toggle">
              <Switch size="small" checked={captureContract} onChange={setCaptureContract} disabled={!!pending}
                aria-labelledby="trial-capture-label" />
              <span id="trial-capture-label">顺带找接口</span>
            </label>}
          </div>
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
            {!explicitSource && captureContract && <EndpointCandidates request={request} url={preview.url}
              candidates={preview.candidates} source={preview.capture_note} open={!samples.length}
              onTrial={trialCandidate} disabled={!!pending} />}
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
          <span role="status" className={feedback ? 'trial-success' : ''}>{feedback || (scopeChanged ?
            (explicitSource ? '配置已修改，请重新试采' : '采集范围已修改，请重新试采') :
            dirty ? '字段规则已修改，请先复测' : '试采样本不会写入采集数据')}</span>
          <Button type="primary" icon={<CheckCircleOutlined />} disabled={!preview?.preview_id || dirty || scopeChanged || !!pending}
            loading={pending === 'enqueue'} onClick={() => void confirm()}>确认加入队列</Button>
        </footer>
      </section>
    </div>
  </>
}

