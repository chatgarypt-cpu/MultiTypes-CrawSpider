import { useEffect, useState } from 'react'
import { Alert, Button, Table, Tag } from 'antd'
import type { TableColumnsType } from 'antd'

type Site = {
  domain: string; incomplete_runs: number; runs: number; measured_runs: number; discovered: number; attempted: number
  saved: number; fetched: number; request_failed: number; rejected: number; acceptance: number | null
  top_reasons: [string, number][]
}

const number = new Intl.NumberFormat('zh-CN')
const measured = (value: number, site: Site) => site.measured_runs ? number.format(value) : '—'
const columns: TableColumnsType<Site> = [
  { title: '站点', dataIndex: 'domain' },
  { title: '异常轮次', dataIndex: 'incomplete_runs' },
  { title: '有详情计数 / 总轮次', render: (_, site) => `${site.measured_runs} / ${site.runs}` },
  { title: '发现', dataIndex: 'discovered', render: measured },
  { title: '尝试', dataIndex: 'attempted', render: measured },
  { title: '取得响应内容', dataIndex: 'fetched', render: measured },
  { title: '请求失败', dataIndex: 'request_failed', render: measured },
  { title: '内容拒收', dataIndex: 'rejected', render: measured },
  { title: '详情通过率', dataIndex: 'acceptance', render: (rate: number | null) => rate === null ? '—' : `${(rate * 100).toFixed(1)}%` },
  { title: '全轮次新增入库', dataIndex: 'saved', render: (value: number) => number.format(value) },
  { title: '主要原因', render: (_, site) => site.top_reasons.map(([reason, n]) => <Tag key={reason}>{reason} · {n}</Tag>) },
]

export default function SiteQuality() {
  const [rows, setRows] = useState<Site[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(false)
    fetch('/api/quality', { signal: controller.signal })
      .then(response => {
        if (!response.ok) throw new Error('quality')
        return response.json()
      })
      .then(data => setRows(data.sites))
      .catch(() => { if (!controller.signal.aborted) setError(true) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [revision])
  return <>
    <p className="panel-caption">后台采集近 7 天的记录。详情通过率按尝试次数计算，重试另计。横线表示缺少可用计数。</p>
    <Button size="small" loading={loading} onClick={() => setRevision(value => value + 1)}>刷新统计</Button>
    {error ? <Alert type="warning" showIcon title="质量统计读取失败，请刷新重试" /> :
      <Table rowKey="domain" size="small" columns={columns} dataSource={rows} loading={loading}
        scroll={{ x: 960 }} pagination={{ defaultPageSize: 10, hideOnSinglePage: true }}
        locale={{ emptyText: '近 7 天尚无后台采集记录' }} />}
  </>
}
