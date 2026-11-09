# collector — 南太平洋信源采集器

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
│   ├── export_csv.py     导出分站 CSV 与合并 CSV
│   ├── status.py         生成 status.json（SSE 数据源）
│   ├── serve.py          看板服务（静态 + SSE + 并发控制 + /csv/）
│   ├── verify.py         采后核对（分页完整性 + 浏览器首尾比对）
│   ├── bb_fetch.mjs      Browserbase CDP 客户端（零依赖）
│   ├── bb_batch.mjs      会话隧道批量 fetch
│   ├── sources.yaml      77 个站点配置
│   └── ui/               React 看板（Vite，构建产物 ui/dist）
├── data/                 运行数据（每站 jsonl / 游标 / 日志 / state.db / 队列状态）
└── output/csv/           交付物：<域名>.csv / .pages.csv / .media.csv / all_sites.csv
```

## 常用命令

```bash
# 单站采集
uv run python collector/collect.py --site <域名> --limit 50
# 页面 / 媒体
... --kind pages | media

# 队列（并发数由看板写 data/queue_config.json）
nohup uv run python collector/run_queue.py >> data/run_queue.out 2>&1 &

# 导出（分站 / 合并）
uv run python collector/export_csv.py --all
uv run python collector/export_csv.py --merged

# 看板
uv run python collector/serve.py      # http://127.0.0.1:8787/
```

## 通道

| channel | 发现方式 | 对象 |
|---|---|---|
| `wp-api` | `/wp-json/wp/v2/{posts,pages,media}` 或 `?rest_route=` | 文章 / 页面 / 媒体元数据 |
| `sitemap` | sitemap.xml（支持 `url_filter`） | 文章 |
| `rss` | feed 条目 | 文章 |
| `html` | 列表页 + `link_pattern`（或 `item_selector` XPath；未配置则跳过） | 文章 |
| `json-list` | 列表 JSON `api_list`（分页 + 可选 `api_categories`）→ 详情页 | 文章 |

`json-list` 支持 `scan_mode: full|incremental`（增量只看前几页新内容）、`known_pages_stop`、
`max_pages_per_run`（单轮翻页上限，避免长期占并发槽）；列表请求复用连接池并按域限速。

出口：`direct`（默认）/ `cloud`（Browserbase `/v1/fetch`）/ `browser`（会话隧道，过 Cloudflare）/ `impersonate`（Chrome TLS 指纹）/ `ego`（本机 Chromium，macOS）/ `playwright`（服务器用 headless Chromium，需 `uv sync --extra browser` + `python -m playwright install --with-deps chromium`）。

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
- domain: www.gov.nu
  channel: html
  base: https://www.gov.nu
  list_urls: [/media-releases, "/media-releases/page:{page}"]
  pages: 20
  link_pattern: '/media-releases/(?!page:)[^/]+$'
  noise_gate: true          # 也可写数字（如 0.65）按站覆盖链接密度阈值
  schema:
    title: "//h1/text()"
  item_selector: "//a[contains(@href,'/media-releases/')]/@href"   # 可选，替代 link_pattern 过滤
```

评估过但暂未采用的库（Scrapling / Crawl4AI / Instructor 等）、14 个 html 站的逐站实测与风险
见 [docs/research/html-pipeline-20261109.md](../docs/research/html-pipeline-20261109.md)。


## 断点

每站一份游标 `data/<域名>/cursor.json`（pages/media 各一份），每批原子写入；`urls.txt` 负责去重；失败不推进游标。队列状态在 `data/queue_done.json` 与 `data/queue_blocked.json`。

---

## 2026-11 重构后（无人值守版）

新增/替换的模块（详见 `docs/REFACTOR_SPEC.md`、`deploy/README.md`）：

| 文件 | 作用 |
|---|---|
| `cli.py` | 统一入口 `collectorctl`：status / deliverables / verify / dedupe / reconcile / check-site / pause / resume / paused / run |
| `deliver.py` | 一条命令产出 `output/csv` + `output/delivery/<yyyymmdd>/` 四件套（manifest / 残差 / 交付说明 / 覆盖率） |
| `supervisor.py` | 常驻守护：按缺口清单补采、同站串行、看门狗（20 分钟零增长杀进程）、stalled 30 分钟重排、暂停/跳过开关 |
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
supervisor 的任务表改为从 `sources.yaml` 派生（wp-api 站自动带 pages/media），`CRAWL_TASKS=fixed` 可回退旧硬编码清单。
