# 成熟采集器架构调研（2026-11-04）

目的：在继续改我们的 `collector/crawler/` 之前，先看成熟项目怎么分层，避免自己发明轮子。

实际 clone 了三个，逐个读了源码（本地 `/tmp/crawl-research/`）：

| 项目 | 定位 | 为什么看它 |
|---|---|---|
| [scrapy/scrapy](https://github.com/scrapy/scrapy) | 通用爬虫框架（Python） | 分层最标准，几乎所有 Python 采集器都在抄它的结构 |
| [apify/crawlee-python](https://github.com/apify/crawlee-python) | 现代爬虫库（Python，带 Playwright） | Request/Queue/Router/Storage 抽象最清楚，适合看"状态与重试"怎么设计 |
| [fhamborg/news-please](https://github.com/fhamborg/news-please) | 新闻站专用采集器 | 和我们的场景最像：sitemap/RSS/GDELT 多入口 + 正文抽取 |

## 一、三家怎么分层

### Scrapy

```
Spiders           每个站点/入口一个 spider，yield Request / Item
Engine            调度中心
Scheduler         请求队列（默认内存/磁盘）
DupeFilter        request_fingerprint() 去重
Downloader        + DownloaderMiddlewares（代理、UA、重试、限速）
SpiderMiddlewares 处理 response -> 过滤
ItemPipeline      清洗 -> 校验 -> 持久化
FeedExporters     导出 CSV/JSON/DB
```

关键点：
- **一切皆 Request**：翻页、详情页、甚至 API 分页，都是 `yield Request(url, callback, meta)`。回调产生新的 Request 或 Item 结束。
- **去重是框架级**：`scrapy/dupefilters.py` 用 `request_fingerprint()`（method + canonical URL + body 的 sha1）做集合，不依赖页面游标。
- **限速在下载器层**：`extensions/throttle.py`（AutoThrottle）按响应延迟动态调整每个 domain 的并发；设 `AUTOTHROTTLE_TARGET_CONCURRENCY`、`DOWNLOAD_DELAY`、`CONCURRENT_REQUESTS_PER_DOMAIN`。
- **站点差异**：写 spider 子类（`start_requests()` + `parse()`），配置走 `custom_settings`。

对我们的启发：**请求级队列 + 请求级去重**，而不是"每页游标 + urls.txt"。我们现在的 cursor 一旦某页失败，整批重来，且无法按单条重试。

### Crawlee (Python)

```
crawlers/      HttpCrawler / BeautifulSoupCrawler / PlaywrightCrawler / AdaptivePlaywrightCrawler
router.py      Router：按 request.label 分发 handler（支持 use() 中间件）
_request.py    Request：url + unique_key + user_data(label, state, retry count, depth)
storages/      RequestQueue / Dataset / KeyValueStore（内存 / 文件 / Redis / SQL 四种后端）
sessions/      会话与指纹轮换
_autoscaling/  动态并发（按 CPU/内存/错误率伸缩）
statistics/    统计与重试计数
fingerprint_suite/  TLS/浏览器指纹
```

关键点：
- `Request` 有**生命周期状态**（`UNPROCESSED/BEFORE_NAV/AFTER_NAV/REQUEST_HANDLER/DONE/ERROR/SKIPPED`）和**每条请求独立的重试次数**（`max_retries`）。
- `Router` 用 `label` 分发（例如 `label="detail"` 走详情 handler，`label="list"` 走翻页 handler）——正是我们"一个通道一个模块"想要的东西。
- `RequestQueue` 有 `forefront`（插队）、`crawl_depth`（深度控制）、失败重入队。
- `Dataset` 逐条 append 并去重，`KeyValueStore` 存游标/状态。

对我们的启发：把"游标 JSON"升级成**请求队列**（每个 URL 一行，带状态与重试次数），失败单条重试，跑完天然可审计。

### news-please

```
crawler/spiders/   newsplease_spider.py（抽象基类，supports_site()）
                   sitemap_crawler.py / rss_crawler.py / recursive_*_crawler.py / gdelt_crawler.py
pipeline/          extractor/（article_candidate.py + extractors/ 多个抽取器 + comparer/ 结果择优）
                   cleaner.py（正文清洗）
helper_classes/    url_extractor.py（找 sitemap/RSS、URL 过滤）
NewsArticle.py     31 字段级别的数据模型
single_crawler.py  单站入口
```

关键点：
- **`ArticleCandidate` + `Comparer`**：多个抽取器（trafilatura / newspaper / readability / jsonld）各产出一个候选，再用 `comparer/` 打分择优。这比我们"trafilatura 抽不到就丢"稳得多。
- **`supports_site(url)`**：蜘蛛自己声明能处理哪些站，入口层自动路由。
- **入口与抽取彻底分离**：spider 只负责发现 URL，抽取在 pipeline 里统一做。

对我们的启发：**抽取质量要显式建模**（候选 + 择优），这正是我们"pages 被 120 字符阈值误杀"的根因；用多抽取器投票可以救回大量短页面。

## 二、我们现在的差距（对照 `collector/crawler/`）

| 维度 | 成熟做法 | 我们现在 | 影响 |
|---|---|---|---|
| 队列/状态 | Request 级队列 + 状态机 | `urls.txt` + 页码游标 | 某页失败→整批重来；无法按条重试；分片脚本内存爆 |
| 去重 | canonical fingerprint（框架级） | URL 字符串集合 | 同文不同 URL（la1ere 301、samoa 分类前缀）会重复 |
| 限速 | 按域自适应（AutoThrottle） | `SITE_WORKERS` 一刀切 | 曾把 mnre/transparencypng 打到 403 |
| 抽取 | 多抽取器候选 + 择优 | 单 trafilatura + 120 字符阈值 | pages 整类被误杀；短稿丢弃 |
| robots.txt | 有中间件 | 完全没处理 | 合规风险（lefigaro 已明确禁止） |
| 管道 | Pipeline 阶段化 | 散在 channel 里 | 校验/清洗/去重混在抓取逻辑里 |
| 入口 | spider 声明 supports_site | YAML 配置（对我们更合适） | 我们 76 个站，配置驱动比写 76 个类合理 |

## 三、建议的 v2 改动（按性价比排序，Ponytail：不引入框架）

### P0 — 请求队列（解决内存 + 断点续跑）

```python
@dataclass
class Request:
    url: str
    channel: str          # wp-api / sitemap / ...
    kind: str = "posts"
    label: str = "detail" # Router 用
    state: str = "unprocessed"   # unprocessed/done/error/skipped
    retries: int = 0
    key: str = ""         # canonical: method+url+params
```

存 sqlite（复用现有 `data/state.db`），`RequestQueue` 提供 `add()/next()/done()/fail()`。
好处：断点续跑、单条重试、分片天然流式（每次只取 500 条），内存问题一并解决。

### P1 — 按域限速 + robots

- 每域名一个 token bucket：默认 2 req/s，连续 403/429 自动降到 0.5 req/s，成功 50 次后回升。
- 启动时抓一次 `robots.txt`，`Disallow` 的 URL 标记 skipped（不抓）。

### P2 — 抽取候选 + 择优

- `extract_candidates(url, html)` 返回 `[Candidate(extractor, text, title, date, score)]`：
  - trafilatura（baseline）
  - JSON-LD Article（结构化字段）
  - readability（`readabilipy` 或 `html2text` 兜底）
- 打分：正文长度、段落数、标题匹配、日期可信度；取最高分。
- **阈值按通道配置**：`min_text` posts=120 / pages=60 / media=0（media 不抽正文）。

### P3 — 管道阶段化

```
fetch -> normalize（URL 规范化） -> validate（必填字段/正文阈值） -> dedupe（canonical key） -> store
```

每阶段可插拔，失败原因单独计数（`error_reason`），这样"为什么这条没入库"可查。

### P4 — 通道配置 schema 化

现在 `sources.yaml` 是自由 dict，容易出现拼错的键（例如 `url_filter` vs `link_pattern` 混用）。给每个 channel 定义一个 dataclass 做校验，启动时一次性校验全表。

## 四、不建议照搬的

- **Scrapy 的 spider 类体系**：我们 76 个站，写 76 个类维护成本太高；配置驱动更适合，且已证明能覆盖 6 类通道。
- **Crawlee 的 autoscaling / 浏览器指纹**：依赖 Playwright/浏览器，我们当前以 HTTP 为主，只保留 `via: browser` 作为逃生口。
- **分布式队列（Redis）**：单机多进程足够，sqlite 队列即可。

## 五、参考位置

- Scrapy 去重：`scrapy/dupefilters.py`、`scrapy/utils/request.py::request_fingerprint`
- Scrapy 限速：`scrapy/extensions/throttle.py`
- Crawlee 请求模型：`crawlee-python/src/crawlee/_request.py`（`RequestState`、`CrawleeRequestData`）
- Crawlee 路由：`crawlee-python/src/crawlee/router.py`
- Crawlee 存储：`crawlee-python/src/crawlee/storages/{_request_queue,_dataset,_key_value_store}.py`
- news-please 蜘蛛：`news-please/newsplease/crawler/spiders/*.py`
- news-please 抽取：`news-please/newsplease/pipeline/extractor/{article_candidate.py,comparer/}`
