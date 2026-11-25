# 网页采集器

一个脚本 + 一张站点表 + 一个状态库。无框架、无数据库服务。

## 依赖与环境

依赖写在仓库根的 [pyproject.toml](../pyproject.toml)，用 uv 管理（Python >= 3.12）：

```bash
uv sync                                  # 建 .venv + 按 uv.lock 装依赖
uv run python collector/collect.py ...   # 用项目环境保护运行
uv run pytest                            # 离线测试（collector/tests）
```

`uv.lock` 固定版本；加依赖改 `pyproject.toml` 后重新 `uv sync`。
需要浏览器抓取（`via: playwright`）时：`uv sync --extra browser && python -m playwright install --with-deps chromium`。
历史 `演练场（ignore)/.venv` 是 launchd/supervisor 正在用的旧环境，依赖与 `.venv` 一致，
两者都能跑；`deploy/collectorctl` 会优先用仓库根 `.venv`。

## 目录

```
REA_CrawlSpider/
├── collector/            代码
│   ├── collect.py        采集（wp-api / sitemap / rss / html，posts|pages|media）
│   ├── run_queue.py      任务队列（可调并发，posts→pages→media 同站串行）
│   ├── export_csv.py     按站点、集合导出 CSV
│   ├── status.py         生成 status.json（SSE 数据源）
│   ├── serve.py          看板服务（静态 + SSE + 并发控制 + /csv/）
│   ├── verify.py         采后核对（分页完整性 + 浏览器首尾比对）
│   ├── bb_fetch.mjs      Browserbase CDP 客户端（零依赖）
│   ├── bb_batch.mjs      会话隧道批量 fetch
│   ├── sources.yaml      管理员维护的站点配置
│   └── ui/               React 看板（Vite，构建产物 ui/dist）
├── data/                 运行数据（每站 jsonl / 游标 / 日志 / state.db / 队列状态）
└── output/csv/           各站导出文件，如 <域名>.csv、<域名>.pages.csv、<域名>.media.csv
```

## 常用命令

```bash
# 单站采集
uv run python collector/collect.py --site <域名> --limit 50
# 页面 / 媒体
... --kind pages | media

# 队列（并发数由看板写 data/queue_config.json）
nohup uv run python collector/run_queue.py >> data/run_queue.out 2>&1 &

# 导出所有站点的各集合，或单独导出某站文章
uv run python collector/export_csv.py --all
uv run python collector/export_csv.py --site <域名>

# 看板
uv run python collector/serve.py      # http://127.0.0.1:8787/
```

## 渠道全景（2026-11-25：29 条，分四层）

一次采集 = **发现 → 取文 →（被挡时）回退 → 提取**。四层各自可换，组合出 29 条渠道；
但能砸开硬墙的只有 3 条半，其余 25 条负责"正常站采得全、采得稳"。

### 1. 发现（7 条）——从哪儿知道有什么文章

| channel | 发现方式 | 对象 |
|---|---|---|
| `wp-api` | `/wp-json/wp/v2/{posts,pages,media}` 或 `?rest_route=` | 文章 / 页面 / 媒体元数据 |
| `sitemap` | sitemap.xml（支持 `url_filter`） | 文章 |
| `rss` | feed 条目 | 文章 |
| `html` | 列表页 + `link_pattern`（或 `item_selector` XPath；未配置则跳过） | 文章 |
| `json-api` | 站点自研 JSON 接口 | 文章 |
| `json-list` | 列表 JSON `api_list`（分页 + 可选 `api_categories`）→ 详情页 | 文章 |
| `archive` | **逆向**：Wayback CDX 里的历史页面清单 | 文章 |

`json-list` 支持 `scan_mode: full|incremental`（增量只看前几页新内容）、`known_pages_stop`、
`max_pages_per_run`（单轮翻页上限，避免长期占并发槽）；列表请求复用连接池并按域限速。

### 2. 取文出口（10 条，`via:`）——用谁的身份、从哪条路去拿

| via | 本质 |
|---|---|
| `direct` | 正常请求（默认） |
| `impersonate` | curl_cffi 伪造 Chrome TLS 指纹 |
| `playwright` | 真 headless Chromium（服务器用，`uv sync --extra browser`） |
| `ego` | 本机常驻 Chrome（macOS） |
| `camoufox` | 反检测 Firefox |
| `browser` | 远端浏览器会话（Browserbase 会话隧道，过 Cloudflare） |
| `cloud` | Browserbase `/v1/fetch` 云取文 |
| `brightdata` | 商业解锁器（对方 IP 池） |
| `proxy` | 自建代理池（http/socks5、每域粘性、失败冷却，见下文「出口 IP」） |
| `archive` | 逆向：只从 wayback 快照取，不碰源站（见下文「逆向采集」） |

> `ego` 复用系统 Chrome 单实例，运行期间本地 Chrome 可能打不开。DSH Web 的浏览器插件
> （ego-cast-worker）会自动拉起 ego，kill 后可能数秒内复活；要彻底停用需在 DSH web profile
> 禁用该插件。日常采集优先 `playwright`；临时释放用 `scripts/stop-ego.sh`（只动 ego lite，
> 不动系统 Chrome/DSH web 服务）。

### 3. 墙后回退（6 条）——正面进不去时换说法

| 源 | 干的事 | 详见 |
|---|---|---|
| `bpc` | Bypass Paywalls Clean 的 416 条站点规则（UA/referer/清 cookie/拦墙脚本） | 下文「BPC 规则」 |
| `googlebot` | 伪装搜索引擎爬虫（新闻站为 SEO 对 Googlebot 放全文） | 下文「付费墙回退」 |
| `freedium` | Medium 专用镜像（`freedium.cfd` 等） | 同上 |
| `archive_org` | wayback `/web/<ts>id_/` 原始快照 | 同上 |
| `archive_today` | `archive.ph/today/is/li` 四个镜像 | 同上 |
| `reader` | r.jina.ai，从对方 IP 抓成 Markdown（对"按出口 IP 限流"的站有效） | 同上 |

回退预算按站配置 `paywall_budget`（默认 80）：整站几百个详情页全失败时（如 `wallis` 的
HTTP/2 `ENHANCE_YOUR_CALM`），预算会在前 80 条就耗光，后面的直接记 `http_0`——所以这类站
要显式调大。`PAYWALL_READER=0` 可关掉 reader 兜底。

### 4. 提取（6 条）——拿到手之后怎么把正文抠出来

| extractor | 触发条件 |
|---|---|
| `trafilatura` | 主路径：剪枝降噪 + Markdown |
| `jsonld` | JSON-LD 里有 `articleBody`（>200 字） |
| `structured_json` | `__NEXT_DATA__` / `__NUXT__` / `__INITIAL_STATE__` 里有全文（前端付费墙常见） |
| `html_text` | 剪枝结果明显不合格（< 粗抽的 40%）时兜底 |
| `reader_markdown` | 第三方 Reader 输出，直接当正文，不进 trafilatura |
| `+meta` | 正文抽不出来时保留题录（标题/日期/描述） |

### 战斗力：这些渠道各能对付什么墙

| 墙的类型 | 有效手段 | 状态 |
|---|---|---|
| Googlebot / 搜索引擎白名单 | `googlebot` UA | ✅ |
| 前端遮罩 / 全文藏在 JSON | `bpc` 规则 + `structured_json` | ✅ |
| 付费墙脚本驱动（meter.js 等） | `bpc` 规则 + Playwright `route.abort` | ✅ NYT 实测 403→200 |
| 计量型（每月 N 篇） | 无 cookie 会话 + 换出口 IP | ✅ |
| 登录墙 / 内容已删 | `archive_org` / `archive_today` | ✅ |
| 按出口 IP 限流/拦截 | `reader`（r.jina.ai）或 `proxy` / `brightdata` | ✅ wallis 实测可用 |
| 真订阅墙 + 无任何存档 | —— | ❌ 无解 |

## 付费墙 / 反爬回退（13ft，2026-11-24）

源项目：[wasi-master/13ft](https://github.com/wasi-master/13ft)（自托管 12ft.io 替代）。它本身是
Flask + 前端状态页 + 反 frame-busting 清理，采集器用不到这些，所以只把**取文链路**移植进
`crawler/paywall.py`，输出与 `Fetcher` 的 `(status, body)` 同形，四个调用点
（`channels/base.py`、`channels/json_list.py`、`web_worker.py`、`web_preview.py` 的试采预览）
在 403/挑战页/付费墙命中时调用它。

链路顺序（与 13ft 一致）：

1. `googlebot` —— 用 Googlebot UA 直取原页面（新闻站为 SEO 对 Googlebot 放全量正文）；
2. `freedium` —— 仅 Medium，走 `freedium-mirror.cfd` / `freedium.cfd`；
3. `archive_org` —— wayback `available` API 找最近快照，取 `/web/<ts>id_/` 原始版本；
4. `archive_today` —— `archive.ph` / `archive.today` / `archive.is` / `archive.li` 的 `newest/`。

判定：正文 ≥ 500 字符、`page_error()` 不命中挑战/错误页、且**正文确有增量**（markdown 比原响应
多 400 字符且超过 1.2 倍）才算恢复成功 —— 只比字符串不够：同一份被截断的页面两次请求只因
nonce/token 不同就不相等，会让 Googlebot 用摘要冒充成功、提前收工。
恢复成功的行照常 `normalize/validate` 入库，队列标签里多一个 `paywall_source`（来源），
`reasons` 里多一个 `paywall_<source>` 计数。**默认关闭**，不改变任何既有站点行为。

```yaml
- domain: news.example.org
  premium_marker: id="impression-panel"
  paywall_fallback: 13ft        # 13ft | googlebot | archive | freedium
```

- `13ft` 全链路；`googlebot` 只伪装 UA 直取；`archive` 只走公开存档（不改 UA）；`freedium` 只走 Medium 镜像。
- 已经另起 13ft 服务（`docker compose up`，默认 9982）时可用 `paywall_fallback: 13ft-service` +
  `paywall_service: http://127.0.0.1:9982`（或环境变量 `PAYWALL_SERVICE_URL`），采集器走它的
  `POST /article`（form `link=<url>`），不重复实现取文逻辑。
- 预算 `PAYWALL_FALLBACK_BUDGET`（默认 80）：单进程每轮最多发起多少次恢复尝试，整站 403 时不会把存档站打爆。
  archive.today 连续两轮全镜像不可达后本轮直接跳过（当前网络实测它常超时，archive.org 正常）。
- 存档站与 Medium 镜像属于站外取文，启用前在 `note:` 里写清原因；Googlebot 直取不受 `robots: false`
  影响，但会按站点开关显式启用。
- 测试：`collector/tests/test_paywall.py`（离线，覆盖候选顺序、挑战页判重、预算、与 `collect_urls` 的对接）。

补采流程（开关只对新抓取生效，历史失败行要先重新入队）：

原站已下线 / 全站 403 的站点，发现阶段拿不到 URL，先用 CDX 把 archive.org 上的历史
页面清单写进队列（`channel=cdx`），再按上面的流程补采：

```bash
.venv/bin/python collector/cdx_seed.py --domain example.org --limit 5000 --dry-run
.venv/bin/python collector/cdx_seed.py --domain example.org --limit 5000
.venv/bin/python collector/collect.py --site example.org --limit 50
```

```bash
# 1) 看哪些失败类型/站点可翻出（不写盘）
.venv/bin/python collector/cli.py requeue --dry-run
# 2) 重新入队：只翻指定站点的受限/403/空响应，404 死链与 PDF 超限不动
.venv/bin/python collector/cli.py requeue --domain public.example.org \
    --error http_0,http_403,access_restricted,no_text
# 3) 单站补采；带关键词的日志里会出现 paywall recovered source=...
.venv/bin/python collector/collect.py --site public.example.org --limit 50
```

## BPC 规则（Bypass Paywalls Clean，2026-11-25）

上游 [magnolia1234/bypass-paywalls-clean-filters](https://gitlab.com/magnolia1234/bypass-paywalls-clean-filters)
（MIT）的站点规则库，**规则一行未改**地搬进来，采集端只负责执行：

```
collector/crawler/bpc_rules/
├── sites.js        上游原文件（70 KB，sha256 记在 upstream.json）
├── LICENSE         MIT, Copyright (c) 2020 magnolia1234
├── upstream.json   来源 / commit / 获取日期 / 校验和 / 更新流程
├── export.mjs      只做 JS→JSON 序列化（RegExp 保留 source/flags）
└── rules.json      416 条规则（allow_cookies 325 / block_regex 227 / useragent 67 / group 66）
```

字段语义照搬上游（`crawler/bpc.py` 里有对照表）：`domain`/`group` 后缀匹配、`excluded_domains`
排除、`useragent: googlebot|bingbot` 换 UA、`referer` 伪造来源、`random_ip` 造 `X-Forwarded-For`、
`remove_cookies` 用无 cookie 会话等效实现、`block_regex` 透出给浏览器路径。

**两层执行**：

| 层 | 能做什么 | 不能做什么 |
|---|---|---|
| HTTP（`bpc.py`） | 换 UA / Referer / XFF、干净会话、命中即作为回退链首位 | 拦不了 `meter.js` 这类脚本 |
| 浏览器（Playwright） | `page.route` 按 `block_regex` 拦截付费墙脚本 + `amp_unhide` 解锁 CSS | 需要 `via: playwright` + `paywall_fallback` |

**实测**：

| 站 | HTTP 层 | 浏览器层（拦 meter.js） |
|---|---|---|
| `thetimes.co.uk` | 200 / 正文 3175 字 | 200 / 3199 字（本来就能过） |
| `nytimes.com` | 403（DataDome） | **200 / 正文 2526 字**（403 → 可读） |

站点开了 `paywall_fallback` 时，`web.py` 的 playwright 分支才会传 `bpc_rules=True`，其他站行为不变。

更新规则：替换 `sites.js` + 更新 `upstream.json` → `node collector/crawler/bpc_rules/export.mjs`。

## 逆向采集（channel: archive / via: archive，2026-11-24）

正向采集依赖站点自己暴露入口（sitemap / RSS / 栏目页 / JSON 接口）。站点下线、全站 403、
接口改版之后这条路就断了，逆向采集反过来从"别人已经存下来的副本"入手：

| 层 | 正向 | 逆向 |
|---|---|---|
| 发现 | 站点 sitemap / 列表页 / API | archive.org **CDX** 历史上的 status=200 HTML 页面 |
| 取文 | 直连站点 | wayback `/web/<ts>id_/` 原始快照 → archive.today `newest/` |
| 提取 | trafilatura + `schema:` | 完全相同（同一套行结构、校验、导出） |

```yaml
- domain: example.org
  channel: archive          # 发现走 CDX
  via: archive              # 取文走存档（不碰源站）
  base: https://example.org
  cdx:
    limit: 5000
    since: 2015             # 可选：只要这份快照之后的历史
    match: "2020|2021"      # 可选：URL 正则过滤
```

- `via: archive` 是**出口**，任何通道都能配（`channel: html` + `via: archive` = 站点列表页还能打开、但正文已付费墙/被删时只从存档取正文）。
- CDX 的过滤（资源文件、查询串、后台路径、空壳页、历史链接拼接）复用 `cdx_seed` 的规则；
  队列里已有待办时不会重复拉 CDX。
- 手动灌历史 URL：`python collector/cdx_seed.py --domain <domain> --limit 5000`。

## 出口 IP / 虚拟 IP 采集（crawler/proxies.py，2026-11-24）

按出口 IP 拦的站（CloudFront 403、部分 Cloudflare 1006）和按 IP 计数的限流，换 UA 没用，
换出口才有用。代理池默认关闭，站点显式声明才生效：

```yaml
- domain: example.org
  via: proxy
  proxy_pool: data/proxies.txt    # 一行一个；也可只写单项 proxy: http://user:pass@host:port
  proxy_sticky: true              # 默认 true：同域固定用同一出口，避免频繁换 IP 触发风控
```

`data/proxies.txt` 支持 `http://`、`https://`、`socks5://`、`socks5h://`、`socks4://` 与裸
`host:port`（按 http 处理），`#` 开头为注释。策略：

- 轮询 + 每域粘性；粘住的出口失败立刻换下一个；
- 连续 `PROXY_MAX_FAILURES`（默认 3）次失败 → 冷却 `PROXY_COOLDOWN`（默认 300s），
  冷却期间不再选中；
- 全池冷却时返回 `None`，该请求回到直连（不会静默丢请求，也不阻塞队列）；
- 日志里代理一律脱敏（`http://***@host:port`），密码不进 `run.log`。

体检（只读，打印每个出口的真实 IP）：

```bash
python collector/proxy_check.py --direct          # 直连出口 IP + 池内每个 IP
python collector/proxy_check.py --file my.txt -v --json
```

注意：`via: proxy` 只覆盖 `direct` / `impersonate` / `archive` 三条取文路径；`playwright` /
`camoufox` / `ego` / `browser`（远端会话）走浏览器自己的网络栈，代理要配在浏览器/CDP 侧，
不经过这里的池。

## HTML 站流水线（2026-11-09）

通用的「渲染抓取 → 剪枝降噪 → 结构化提取」三层里，抓取层沿用上面的出口，另外两层在
`crawler/htmlpage.py` 实现（不新增运行时依赖）：

1. **剪枝降噪**：trafilatura 抽取正文并输出 Markdown；`link_ratio`（可见文字落在 `<a>` 里的比例）
   判定列表页/导航页，配 `noise_gate: true` 时这类页面直接丢弃，不再当文章入库。
   同时修正了 `extract.py` 的择优逻辑：整页粗抽只在剪枝结果明显不合格（<40%）时兜底，
   避免"含导航的整页文字更长所以永远胜出"。
2. **结构化提取**：`schema:`（字段 → XPath）做确定性字段覆盖；`llm_fields:` 在配置了
   `LLM_API_KEY`（或 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`，可配 `LLM_BASE_URL` / `LLM_MODEL`）
   时用任意 OpenAI 兼容接口补空字段，未配置 key 时完全不参与（离线测试不联网）。

```yaml
- domain: example.org
  channel: html
  base: https://example.org
  list_urls: [/media-releases, "/media-releases/page:{page}"]
  pages: 20
  link_pattern: '/media-releases/(?!page:)[^/]+$'
  noise_gate: true          # 也可写数字（如 0.65）按站覆盖链接密度阈值
  schema:
    title: "//h1/text()"
  item_selector: "//a[contains(@href,'/media-releases/')]/@href"   # 可选，替代 link_pattern 过滤
```



## 断点

每站一份游标 `data/<域名>/cursor.json`（pages/media 各一份），每批原子写入；`urls.txt` 负责去重；失败不推进游标。队列状态在 `data/queue_done.json` 与 `data/queue_blocked.json`。

---

## 2026-11 重构后（无人值守版）

新增/替换的模块（详见 `docs/REFACTOR_SPEC.md`、`deploy/README.md`）：

| 文件 | 作用 |
|---|---|
| `cli.py` | 统一入口 `collectorctl`：status / deliverables / verify / dedupe / reconcile / check-site / pause / resume / paused / run |
| `deliver.py` | 一条命令产出 `output/csv` + `output/delivery/<yyyymmdd>/` 四件套（manifest / 残差 / 交付说明 / 覆盖率） |
| `supervisor.py` | 常驻守护。跟踪请求日志、游标和落盘进度。无进展时终止进程组，连续失败达到重试预算后停在 stalled。移除按站暂停条目后恢复。 |
| `health.py` | 每 30s 写 `data/health.json`（吞吐/队列/错误率/最近错误），供告警与看板读取 |
| `crawler/cursor.py` | 游标状态机：`complete / partial / blocked / paused / interrupted`，只有真正翻完才算 complete |
| `crawler/reconcile.py` | 队列与磁盘对账（urls.txt 已有但队列仍 unprocessed → 标 done） |
| `crawler/store.py` | 写入去重（canonical url 唯一，写前查 known 缓存） |
| `deploy/` | launchd plist（KeepAlive）+ cron 示例 + collectorctl 包装脚本 |

常用命令：

```bash
PY=".venv/bin/python"                         # uv sync 生成；旧环境见上
"$PY" collector/cli.py status                 # 进度总览
"$PY" collector/cli.py check-site <domain>    # 干跑发现，不入库
"$PY" collector/collect.py --check-extract <domain> --samples 8   # 干跑发现+抽样抽取验收
"$PY" collector/backfill_gaps.py --site <domain> --kind posts     # 按源站清单补缺口（wp-api）
"$PY" collector/cli.py paused                 # 查看暂停/守护状态
"$PY" collector/cli.py run                    # 常驻守护（= collector/supervisor.py）
"$PY" collector/cli.py deliverables           # 导出 CSV + 生成交付包
"$PY" -m pytest                               # 131 条离线测试
```

安全开关：`data/CRAWL_PAUSED`（全局暂停）、`data/CRAWL_SKIP`（按 `domain:kind` 跳过）。

配置里的 `disabled: true` + `disabled_reason` 表示"人工确认不采"（站点下线/无列表入口/反爬），
批量采集与 supervisor 都会跳过它，看板单独归类为"缺入口或不可访问"；`collect.py --site X` 仍可临时单独跑。
supervisor 只调度本机配置中启用的站点。配置为空时，待采队列也为空。

## 后台导出与站点质量

守护进程和定时队列以 `--no-csv` 启动采集子进程。站点监控的 CSV 下载按 JSONL 的更新时间生成文件，交付命令可显式导出。网页任务继续生成各集合的 CSV，停止后也可下载已保存的条目。

「站点监控 → 近 7 天质量」读取后台 `state.db` 中的运行记录。`health.json` 的 `site_quality_7d` 使用相同口径。轮次按完成时间归入窗口，每次通道执行单独计数。异常轮次包含 partial、interrupted、blocked 和 failed。新增入库按每轮实际写入的记录数求和。

| 指标 | 计数方式 |
| --- | --- |
| 已登记文章 URL | 各站文章集合 `urls.txt` 的行数。健康快照的 `collection_index_rows` 汇总所有集合，缺索引时使用该集合 JSONL 行数。兼容字段 `total_rows` 使用同一口径。 |
| 发现、尝试 | 新版链接详情通道和 JSON 列表分别记录发现的候选条数、实际提交的详情尝试次数。重新发现和重试均可再次计数。 |
| 请求失败、内容拒收 | 按本轮的详情结果分别计数。请求失败包括无响应和 HTTP 错误，拒收包括正文校验、访问受限和解析失败。 |
| 详情通过率 | （尝试 − 请求失败 − 内容拒收）/ 尝试。通过校验的重复 URL 可以不新增入库。 |
| 主要原因 | 按本窗口内各类失败或拒收出现的次数，显示最多三项。旧记录和分页 API 的轮次、入库数照常展示，缺少详情计数时显示横线。 |
