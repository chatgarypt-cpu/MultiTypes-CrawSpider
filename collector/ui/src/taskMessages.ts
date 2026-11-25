const collectionNames = new Map([
  ['posts', '文章'], ['pages', '页面'], ['media', '媒体'], ['web_pages', '网页'],
])

const collectionStatuses = new Map([
  ['complete', '已完成'], ['partial', '不完整'], ['blocked', '访问受阻'],
  ['interrupted', '已中断'], ['failed', '失败'], ['paused', '已暂停'], ['running', '采集中'],
])

const reasons = new Map([
  ['discovery_failed', '找不到可用采集入口'],
  ['dead_letters', '部分链接采集失败'],
  ['no_output', '没有采到新内容'],
  ['pending', '还有内容待采集'],
  ['limit', '达到本轮采集上限'],
  ['run_limit', '达到本轮采集上限'],
  ['skipped_pages', '部分页面采集失败'],
  ['non_json', '站点返回格式异常'],
  ['bad_json', '站点返回格式异常'],
  ['bad_json_schema', '站点返回格式异常'],
  ['no_text', '未提取到正文'],
  ['short_text', '正文内容过短'],
  ['extraction_failed', '正文提取失败'],
  ['archive_miss', '未找到存档'],
])

export const collectionName = (kind: string) => collectionNames.get(kind) ?? kind
export const collectionStatus = (status?: string) => collectionStatuses.get(status ?? '') ?? '状态未知'

export function taskStage(stage: string) {
  const collection = /^(采集|补采|合并) ([a-z][\w-]*)$/i.exec(stage)
  return collection ? collection[1] + collectionName(collection[2]) : stage
}

function issueLabel(error: string) {
  if (/response_size_limit|exceeds response size limit/i.test(error)) return '文件超出大小限制'
  if (/timed? ?out|超时/i.test(error)) return '连接站点超时'
  const status = Number(/^(?:first page\s+|(?:(?:起始页|浏览器)返回 )?http[_\s]+)(-?\d+)\b/i.exec(error)?.[1])
  if (status === 404 || status === 410) return '采集入口不存在'
  if (status === 401 || status === 403) return '站点拒绝访问'
  if (status === 429) return '请求过于频繁'
  if (status >= 500) return '站点服务异常'
  if (status <= 0) return '暂时无法连接站点'
  if (status === 200 && /^first page /i.test(error)) return '站点返回格式异常'
  if (/正在采集/.test(error)) return '该集合正在采集'
  if (/全局暂停/.test(error)) return '采集已全局暂停'
  if (/集合已暂停/.test(error)) return '所选集合已暂停'
  if (/^(任务已停止|任务进程已中断)/.test(error)) return '任务已中断'
  if (/^部分内容未采完/.test(error)) return '部分内容未采完'
  return reasons.get(error.toLowerCase())
}

export function taskIssue(error: string | undefined, fallback: string) {
  if (!error?.trim()) return ''
  const labels = new Set(error.split(/[；;]/).map(part => part.trim()).filter(Boolean)
    .map(part => issueLabel(part) ?? fallback))
  return labels.size === 1 ? [...labels][0] : fallback
}
