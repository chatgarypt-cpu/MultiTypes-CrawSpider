import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import EndpointCandidates from './EndpointCandidates'
import type { Candidate } from './EndpointCandidates'
import TrialPanel from './TrialPanel'
import { api } from './CrawlPanel'

const url = 'https://example.test/'
const candidate: Candidate = {
  endpoint: 'GET example.test/api/v2/articles',
  channel: 'json-api',
  method: 'GET',
  host: 'example.test',
  path_template: '/api/v2/articles',
  pagination: { param: 'page', values: [1, 2] },
  fields: { data: { type: 'list' }, title: { type: 'string' }, id: { type: 'number' }, date: { type: 'string' } },
  score: 65.7,
  reasons: ['已找到列表字段'],
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  vi.stubGlobal('matchMedia', (media: string) => ({
    media, matches: false, addEventListener() {}, removeEventListener() {},
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('接口线索', () => {
  it('显示页面脚本没有候选的空态', () => {
    render(<EndpointCandidates source="html" url={url} request={api} open />)
    expect(screen.getByText('页面脚本里没找到接口')).toBeTruthy()
    expect(screen.queryByRole('list')).toBeNull()
  })

  it('显示接口、分数、字段摘要和分页参数，理由通过提示展开', async () => {
    render(<EndpointCandidates candidates={[candidate]} source="har" url={url} request={api} open />)
    expect(screen.getByText(candidate.endpoint)).toBeTruthy()
    expect(screen.getByText('JSON 接口')).toBeTruthy()
    expect(screen.getByText('分页参数 page')).toBeTruthy()
    expect(screen.getByText('字段 data、title、id 等 4 个')).toBeTruthy()
    expect(screen.queryByText('已找到列表字段')).toBeNull()
    fireEvent.focus(screen.getByText('65.7 分'))
    expect((await screen.findByRole('tooltip')).textContent).toContain('已找到列表字段')
  })

  it('没有分页时显示未发现', () => {
    render(<EndpointCandidates candidates={[{ ...candidate, pagination: null }]} source="html" url={url} request={api} open />)
    expect(screen.getByText('分页参数 未发现')).toBeTruthy()
  })

  it('将候选和域名提交给看板，并复制服务端生成的 YAML', async () => {
    const yaml = '- domain: example.test\n'
    const fetchMock = vi.fn().mockResolvedValue(Response.json({ yaml, warnings: [], errors: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const writeText = vi.fn().mockResolvedValue(undefined)
    const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      render(<EndpointCandidates candidates={[candidate]} source="html" url={url} request={api} open />)
      fireEvent.click(screen.getByRole('button', { name: '复制配置' }))
      await waitFor(() => expect(writeText).toHaveBeenCalledWith(yaml))
      expect(fetchMock).toHaveBeenCalledWith('/api/contract/entry', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ candidate, domain: 'example.test' }),
      })
      expect(await screen.findByText('已复制')).toBeTruthy()
    } finally {
      if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
      else Reflect.deleteProperty(navigator, 'clipboard')
    }
  })
})

it('默认用布尔值开启接口线索，关闭后连续试采均省略该字段', async () => {
  const fetchMock = vi.fn(async (_path: string, _options: RequestInit) => Response.json({
    url, channel: 'html', rules: {}, samples: [], issues: [], source: url, transport: 'direct',
  }))
  vi.stubGlobal('fetch', fetchMock)
  render(<TrialPanel request={api} enqueue={vi.fn()} />)
  fireEvent.change(screen.getByRole('textbox', { name: '站点或文章网址' }), { target: { value: url } })
  const toggle = screen.getByRole('switch', { name: '顺带找接口' })
  const preview = screen.getByRole('button', { name: /试采样本/ }) as HTMLButtonElement
  expect(toggle.getAttribute('aria-checked')).toBe('true')
  fireEvent.click(preview)
  await waitFor(() => expect(preview.disabled).toBe(false))
  expect(JSON.parse(String(fetchMock.mock.calls[0][1].body)).capture_contract).toBe(true)

  fireEvent.click(toggle)
  for (const index of [1, 2]) {
    fireEvent.click(preview)
    await waitFor(() => expect(preview.disabled).toBe(false))
    expect(fetchMock.mock.calls[index][0]).toBe('/api/preview')
    expect(JSON.parse(String(fetchMock.mock.calls[index][1].body))).not.toHaveProperty('capture_contract')
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  }
})

it('从候选试采 JSON 列表，只允许确认新样本，编辑详情模板后须复测', async () => {
  const listCandidate = { ...candidate, channel: 'json-list',
    evidence: { sample_url: url + 'api/v2/articles?page=1&lang=fr' } }
  const oldPreview = { url, channel: 'html', rules: {}, samples: [], issues: [], source: url, transport: 'direct',
    preview_id: 'old-preview', candidates: [listCandidate], capture_note: 'html' }
  const newPreview = { ...oldPreview, channel: 'json-list', candidates: [], preview_id: 'list-preview',
    samples: [{ url: url + 'news/1', title: 'Example article', text: 'Sample body', text_length: 11, fields: {} }] }
  const fetchMock = vi.fn(async (_path: string, _options: RequestInit) => Response.json(oldPreview))
  fetchMock.mockImplementationOnce(async () => Response.json(oldPreview))
  fetchMock.mockImplementation(async () => Response.json(newPreview))
  vi.stubGlobal('fetch', fetchMock)
  const enqueue = vi.fn().mockResolvedValue('已加入队列')
  render(<TrialPanel request={api} enqueue={enqueue} />)
  fireEvent.change(screen.getByRole('textbox', { name: '站点或文章网址' }), { target: { value: url } })
  fireEvent.click(screen.getByRole('button', { name: /试采样本/ }))
  fireEvent.click(await screen.findByRole('button', { name: '试采此接口' }))
  expect(await screen.findByText('Example article')).toBeTruthy()
  const payload = JSON.parse(String(fetchMock.mock.calls[1][1].body))
  expect(payload).toMatchObject({ channel: 'json-list',
    api: { path: '/api/v2/articles?page=1&lang=fr', page_param: 'page', detail_path: '/api/v2/articles/{id}' },
    scope: { mode: 'default' } })
  expect(payload).not.toHaveProperty('capture_contract')

  const confirm = screen.getByRole('button', { name: /确认加入队列/ }) as HTMLButtonElement
  expect(confirm.disabled).toBe(false)
  fireEvent.change(screen.getByRole('textbox', { name: '详情路径模板' }), { target: { value: '/news/{id}' } })
  expect(confirm.disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: /试采样本/ }))
  await waitFor(() => expect(confirm.disabled).toBe(false))
  fireEvent.click(confirm)
  await waitFor(() => expect(enqueue).toHaveBeenCalledWith('list-preview'))
})
