# 采集器重构规格（2026-11-07）

> 给 Agent Team 的共享规格。所有实现必须遵守本文件里的接口与边界；如需变更接口，先在团队消息里告知 Lead，不要各改各的。

## 0. 环境与约束

- 仓库：`/Users/gary/项目开发/REA_CrawlSpider`，Python 3.14
- 解释器：`/Users/gary/项目开发/REA_CrawlSpider/演练场（ignore）/.venv/bin/python`（含 requests / pyyaml / trafilatura / curl_cffi / httpx / pytest）
- 跑测试：`cd /Users/gary/项目开发/REA_CrawlSpider && "演练场（ignore）/.venv/bin/python" -m pytest -q`
- **禁止**：改 `data/`、`output/` 下的数据；改 `sources.yaml` 语义（T3 只做只读校验增强）；启动真实抓取任务；kill 进程；引入 pytest 之外的新依赖。
- 测试必须离线（fixture/临时目录），不得访问外网。
- 向后兼容：现有 CLI 参数（`collect.py --site/--kind/--limit/--validate/--all`）与函数签名（见下）不能破坏，桌面看板 `serve.py` 还要能跑。

## 1. 今天的真实事故（测试要覆盖到的点）

1. `mark_error` 曾整站 UPDATE，把该域所有 done 行重置为 unprocessed → 整站重复抓取、重复入库（matangitonga 2,768 条重复）。
2. `append_rows` 无去重、无唯一约束 → lnc 重复 16,150 条。
3. 空页/挑战页/发现为 0 都曾被当成"完成"，导致假完成（cookislands、wallis、fnu）。
4. `sources.yaml` 重复 key 被静默覆盖（`list_urls`、`link_pattern`）。
5. `robots.txt` 未按 User-agent 分组，把 GPTBot 的 `Disallow: /` 当全站禁止。
6. `wp_fields` 被误用于 media 接口，过滤掉 `source_url` → media 全空。
7. 无连接池、body 无上限：单条慢速滴流把批次卡死 65 分钟。
8. `discover.py` 里有重复函数定义（后定义覆盖前定义）。

## 2. 接口契约（四组）

### 2.1 store（T1 负责）

```python
# crawler/store.py
append_rows(domain: str, rows: list[dict], kind: str = "posts") -> int
    """追加入库；必须对 url 去重（同 domain+kind 内 canonical_key 唯一），返回实际写入行数。"""
load_known(domain: str, kind: str = "posts") -> set[str]
    """磁盘上已有的 url 集合（保持现有行为）。"""

# crawler/reconcile.py（新增）
reconcile(domain: str | None = None, dry_run: bool = False) -> dict
    """把 state.db 里 state='unprocessed' 但 url 已在 urls.txt 里的行标为 done；
    返回 {"updated": n, "by_domain": {...}}。dry_run 只统计不写。"""
```

要求：
- `append_rows` 去重实现必须**进程内缓存 known 集合**（首次加载 urls.txt，追加时同步更新缓存），不能每行重读文件；100 万行量级下仍要可用。
- `request.RequestQueue` 连接：`PRAGMA journal_mode=WAL`、`busy_timeout=30000`、`synchronous=NORMAL`；`mark_error` 只影响传入 key（已修，加测试锁死）。

### 2.2 cursor（T2 负责）

```python
# crawler/cursor.py（新增）
STATUS = {complete, partial, blocked, paused, interrupted}
load(domain, kind) -> dict          # 兼容旧文件；缺 status 时按 done 推导
save(domain, kind, **fields) -> dict
mark_complete(domain, kind, **extra)
mark_partial(domain, kind, reason, **extra)   # 有缺量但已确认不可得
mark_blocked(domain, kind, reason)            # 403/登录墙/站点不可用
is_complete(domain, kind) -> bool
```

要求：
- 文件仍是 `data/<domain>/cursor.json` / `<kind>.cursor.json`，JSON 可读；保留 `next_page/total_pages/saved/done` 字段（`done == (status=="complete")`）。
- `channels/wp_api.py::collect_wp` 与 `channels/base.py::collect_urls` 改造后必须：空页重试 N 次后**跳过该页**（不标 complete）；只有翻过 `total_pages`（或 400 invalid page）且末页非空 → complete；发现阶段 0 条 URL → 不 complete（标记 interrupted/partial）。
- 每 URL 的失败原因计数要写进 cursor 的 `reasons`（如 `{"http_404": 12, "no_text": 3}`），不再只有内存 Counter。

### 2.3 fetch（T3 负责）

保持现有公开函数签名（`web.get_text / Fetcher.get_many / Fetcher.stream_many / web.wp_json / web._get`），内部统一：

```python
# crawler/web.py
get_text(url, headers, timeout=(10, 25)) -> (status, text)   # 流式、体积/时延上限、非文本跳过
Fetcher.get_many(urls, timeout=60)                            # 走全局 Session（连接池），保序返回
Fetcher.stream_many(urls, workers=4)                          # 并发 + 限速，超时单条失败不拖累整批
```

要求：
- 模块级全局 `requests.Session` + `HTTPAdapter(pool_connections=16, pool_maxsize=32)`；所有直连请求复用它。
- 超时/体积：connect=10s、read=25s；body 上限 8MB、总时延 20s（可用环境变量覆盖）。
- 重试：连接错误/5xx 最多 2 次、指数退避；403/429 交给 `politeness` 惩罚。
- `politeness.RobotsCache`：按 User-agent 分组解析（只认 `*` 组，保留现有实现）、支持 `Crawl-delay`（每域最小间隔）、robots 结果**持久化缓存**到 `data/robots.json`（TTL 24h）。

### 2.4 deliver / CLI / supervisor / health（T4 负责）

```bash
# 统一入口 collector/cli.py（也可 python collector/cli.py ...）
collectorctl status
collectorctl deliverables [--date 20261107] [--no-export]
collectorctl verify [--json]
collectorctl dedupe [--all] [--dry-run]
collectorctl reconcile [--domain X] [--dry-run]
collectorctl check-site <domain>          # 干跑：只做发现，打印 N 条 URL，不入库
collectorctl pause | resume | paused
collectorctl run                          # 常驻守护（= collector/supervisor.py）
```

- `deliverables` 产出：`output/csv/`（复用 `export_csv.py`）+ `output/delivery/<yyyymmdd>/{site_manifest.csv, residuals.csv, 交付说明.md, coverage.json}`。残差/覆盖率直接读 `data/coverage-*.json`（无则先跑 verify_coverage）与 urls.txt 行数。
- `supervisor.py`：把 `演练场（ignore）/supervise.py` 的逻辑收进仓库（暂停开关 `data/CRAWL_PAUSED`、按站跳过 `data/CRAWL_SKIP`、看门狗 20 分钟零增长杀进程、stalled 任务 30 分钟重排、空闲常驻、`data/supervise_state.json` 状态）。
- `health.py`：每次循环写 `data/health.json`：`{ts, pid, running:[...], pending, stalled, done, paused, total_rows, rate_5m, errors_1h, last_error}`。
- `deploy/`：launchd plist（KeepAlive）+ 安装说明；cron 示例（每日增量、每周 verify+deliver、每月 dedupe）。

## 3. 测试要求

- 每个任务至少 6 个测试：happy path + 今天事故对应的回归用例 + 边界。
- 用 `tmp_path` + `monkeypatch` 把 `crawler.config.DATA` / `crawler.store.DATA` / `crawler.request.DB` 指到临时目录（注意模块里直接 import 的常量要一起 patch）。
- 禁止 `time.sleep` 超过 0.1s；禁止真实网络。
- 完成后跑全量 pytest，确保全绿。

## 4. 协作规则

- 只改自己任务写作用域里的文件；需要改别人的文件先发消息给 Lead。
- 新增文件放 `collector/` 下对应目录；测试放 `collector/tests/`。
- 完成后在 `team_task_update` 里 complete，并 `send_message` 给 lead：改了什么、测试结果、剩余风险。
- 遇到接口歧义：按本文件为准；本文件没写的，选最小改动并告知 Lead。
