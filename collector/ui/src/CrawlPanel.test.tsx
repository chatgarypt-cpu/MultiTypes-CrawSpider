import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import CrawlPanel from './CrawlPanel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('订阅列表按已启用的信源计数，加入订阅任务出现在订阅记录中', async () => {
  vi.stubGlobal('matchMedia', (media: string) => ({
    media, matches: false, addEventListener() {}, removeEventListener() {},
  }))
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ jobs: [{
    id: 'reg', url: 'https://waiting.test/', action: 'register', status: 'queued',
    stage: '等待加入长期采集', created: 1, saved: 0, files: [],
  }] })))
  const source = { domain: 'subscribed.test', base_url: 'https://subscribed.test/news', saved: 12, needs_config: false }
  await act(async () => {
    render(<CrawlPanel registeredRuns={[source]} subscriptions={[source]} />)
  })
  fireEvent.click(screen.getByRole('tab', { name: '订阅列表 (1)' }))
  const tab = screen.getByRole('tabpanel')
  const tables = within(tab).getAllByRole('table')
  expect(within(tables[0]).getByRole('link', { name: source.base_url }).getAttribute('href')).toBe(source.base_url)
  expect(within(tables[0]).getByText('采集中')).toBeTruthy()
  expect(within(tables[0]).queryByText('https://waiting.test/')).toBeNull()
  expect(within(tables[1]).getByText('https://waiting.test/')).toBeTruthy()
}, 15_000)
