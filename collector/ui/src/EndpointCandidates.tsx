import { useState } from 'react'
import { Button, Collapse, Input, Modal, Tag, Tooltip, Typography, message } from 'antd'
import { CopyOutlined, InfoCircleOutlined } from '@ant-design/icons'

export type Candidate = {
  endpoint: string
  channel: string
  method?: string
  host?: string
  path_template?: string
  query?: Record<string, string[]>
  pagination?: { param: string; values?: (number | string)[]; confidence?: number } | null
  fields?: Record<string, { type: string; completeness?: number }>
  sample_count?: number
  score?: number
  reasons?: string[]
  evidence?: { sample_url?: string; status_codes?: number[]; time_range?: string[] }
}

export type CandidateSource = 'har' | 'html' | 'unavailable'

type ConfigResponse = { yaml?: string; errors: string[]; warnings: string[] }
type ConfigRequest = (path: string, body: unknown) => Promise<ConfigResponse>

const channelNames: Record<string, string> = {
  'json-api': 'JSON 接口',
  'json-list': 'JSON 列表',
}
const emptyNotes = {
  har: '抓包没有发现接口线索',
  html: '页面脚本里没找到接口',
  unavailable: '没找到接口线索（服务端无浏览器或页面无脚本）',
}

function CandidateRow({ candidate, source, domain, request, notify, onTrial, disabled }: {
  candidate: Candidate
  source?: CandidateSource
  domain?: string
  request: ConfigRequest
  notify: ReturnType<typeof message.useMessage>[0]
  onTrial?: (candidate: Candidate) => void
  disabled?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [warnings, setWarnings] = useState<string[]>([])
  const [manualCopy, setManualCopy] = useState('')
  const fields = Object.keys(candidate.fields ?? {})

  async function copy() {
    setBusy(true)
    setWarnings([])
    try {
      const result = await request('/api/contract/entry', { candidate, domain: candidate.host ?? domain })
      setWarnings(result.warnings)
      if (result.errors.length || !result.yaml) {
        void notify.warning(result.errors[0] || '未能生成配置')
        return
      }
      try {
        await navigator.clipboard.writeText(result.yaml)
        void notify.success('已复制')
      } catch {
        setManualCopy(result.yaml)
      }
    } catch (error) {
      void notify.warning(error instanceof Error ? error.message : '配置生成失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const reasons = candidate.reasons ?? []
  return <li className="trial-candidate">
    <div className="trial-candidate-main">
      <code>{candidate.endpoint}</code>
      <div className="trial-candidate-details">
        <Tag>{channelNames[candidate.channel] || candidate.channel}</Tag>
        {typeof candidate.score === 'number' && <Tooltip
          title={reasons.length ? reasons.map(reason => <div key={reason}>{reason}</div>) : undefined}
          trigger={['hover', 'focus']}>
          <Tag tabIndex={reasons.length ? 0 : undefined}>{candidate.score} 分</Tag>
        </Tooltip>}
        <small>分页参数 {candidate.pagination?.param || '未发现'}</small>
        {source === 'har' && !!candidate.sample_count && <small>{candidate.sample_count} 条响应样本</small>}
      </div>
      {!!fields.length && <small className="trial-candidate-fields">
        字段 {fields.slice(0, 3).join('、')}{fields.length > 3 && ` 等 ${fields.length} 个`}
      </small>}
    </div>
    <div className="trial-candidate-actions">
      {!!warnings.length && <Tooltip title={warnings.map(warning => <div key={warning}>{warning}</div>)}
        trigger={['hover', 'focus']}>
        <Button type="text" size="small" icon={<InfoCircleOutlined />} aria-label="配置提示" />
      </Tooltip>}
      {onTrial && channelNames[candidate.channel] && candidate.path_template &&
        <Button size="small" disabled={disabled} onClick={() => onTrial(candidate)}>试采此接口</Button>}
      <Button type="text" size="small" icon={<CopyOutlined aria-hidden />} loading={busy}
        disabled={disabled} onClick={() => void copy()}>复制配置</Button>
    </div>
    <Modal title="复制配置" open={!!manualCopy} onCancel={() => setManualCopy('')} footer={null}>
      <Input.TextArea aria-label="信源配置 YAML" value={manualCopy} readOnly autoSize={{ minRows: 5, maxRows: 12 }}
        onFocus={event => event.currentTarget.select()} />
    </Modal>
  </li>
}

export default function EndpointCandidates({ candidates = [], source, open, request, url, onTrial, disabled }: {
  candidates?: Candidate[]
  source?: CandidateSource
  open: boolean
  request: ConfigRequest
  url: string
  onTrial?: (candidate: Candidate) => void
  disabled?: boolean
}) {
  const [notify, context] = message.useMessage()
  if (!source && !candidates.length) return null
  let domain: string | undefined
  try { domain = new URL(url).host } catch { /* The candidate still has its own host. */ }

  return <>
    {context}
    <Collapse className="trial-diagnostics" defaultActiveKey={open && candidates.length <= 3 ? ['endpoints'] : []}
      items={[{
        key: 'endpoints',
        label: <span>接口线索 {source === 'har' && <Tag>来自抓包，可信度更高</Tag>}</span>,
        children: candidates.length ? <ul className="trial-candidates">
          {candidates.map(candidate => <CandidateRow key={candidate.endpoint}
            candidate={candidate} source={source} domain={domain} request={request} notify={notify}
            onTrial={onTrial} disabled={disabled} />)}
        </ul> : <Typography.Text type="secondary">{source ? emptyNotes[source] : '未发现接口线索'}</Typography.Text>,
      }]} />
  </>
}
