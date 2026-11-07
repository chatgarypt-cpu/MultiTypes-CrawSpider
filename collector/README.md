# collector — 南太平洋信源采集器

一个脚本 + 一张站点表 + 一个状态库。无框架、无数据库服务。

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
uv run --with requests --with pyyaml --with trafilatura python3 collector/collect.py --site <域名> --limit 50
# 页面 / 媒体
... --kind pages | media

# 队列（并发数由看板写 data/queue_config.json）
nohup uv run --with requests --with pyyaml --with trafilatura --with curl_cffi \
  python3 collector/run_queue.py >> data/run_queue.out 2>&1 &

# 导出（分站 / 合并）
python3 collector/export_csv.py --all
python3 collector/export_csv.py --merged

# 看板
python3 collector/serve.py            # http://127.0.0.1:8787/
```

## 通道

| channel | 发现方式 | 对象 |
|---|---|---|
| `wp-api` | `/wp-json/wp/v2/{posts,pages,media}` 或 `?rest_route=` | 文章 / 页面 / 媒体元数据 |
| `sitemap` | sitemap.xml（支持 `url_filter`） | 文章 |
| `rss` | feed 条目 | 文章 |
| `html` | 列表页 + `link_pattern`（未配置则跳过） | 文章 |

出口：`direct`（默认）/ `cloud`（Browserbase `/v1/fetch`）/ `browser`（会话隧道，过 Cloudflare）/ `impersonate`（Chrome TLS 指纹）。

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
WS="演练场（ignore）"; PY="$WS/.venv/bin/python"
"$PY" collector/cli.py status                 # 进度总览
"$PY" collector/cli.py check-site <domain>    # 干跑发现，不入库
"$PY" collector/cli.py paused                 # 查看暂停/守护状态
"$PY" collector/cli.py run                    # 常驻守护（= collector/supervisor.py）
"$PY" collector/cli.py deliverables           # 导出 CSV + 生成交付包
"$PY" -m pytest                               # 102 条离线测试
```

安全开关：`data/CRAWL_PAUSED`（全局暂停）、`data/CRAWL_SKIP`（按 `domain:kind` 跳过）。
