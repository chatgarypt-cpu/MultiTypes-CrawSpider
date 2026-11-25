# 采集器架构（模块化后）

## 目录

```
collector/
  collect.py              # CLI（很薄）：参数解析 + 遍历站点 + 打印结果
  crawler/
    config.py             # 路径、UA、31 列 schema、并发数
    text.py               # HTML/文本小工具（clean、meta、日期解析…）
    extract.py            # 正文抽取（trafilatura + JSON-LD 兜底）
    htmlpage.py           # HTML 后两层：剪枝→Markdown、列表页闸门、XPath schema、可选 LLM 补字段
    mapping.py            # 各通道原始对象 → 31 列行（map_wp_post / map_wp_media / map_json_item）
    discover.py           # 找 URL：sitemap / rss / html 列表 / WP 分页
    web.py                # HTTP 层：Fetcher（direct/impersonate/cloud/browser）、_get、wp_json
    store.py              # 落盘：jsonl 追加、urls 去重、游标、sqlite run 日志
    runner.py             # 单站调度：按 channel 选模块，写 run 记录，刷新 CSV
    request.py            # Request + RequestQueue（sqlite，状态机 / 单条重试 / 断点续跑）
    politeness.py         # 按域自适应限速 + robots.txt 闸门
    pipeline.py           # normalize → validate（按通道阈值）
    schema.py             # sources.yaml 结构校验（--validate）
    channels/
      __init__.py         # 注册表：channel 名 → run()
      base.py             # URL 列表型通道的共用循环（fetch → extract → store）
      wp_api.py           # WordPress REST（posts/pages/media，分页 + 游标）
      sitemap.py          # sitemap 索引/子图 → 文章 URL
      rss.py              # RSS/Atom → 文章 URL
      html_list.py        # 列表页 + link_pattern（支持多 list_url、{offset} 分页）
      json_api.py         # Directus 风格 {data, meta.total_count}
      json_list.py        # 列表 JSON + 详情页（按 free_field 过滤付费文）
```

## 五个通用维度（2026-11-04 重构）

| 维度 | 实现 | 作用 |
|---|---|---|
| P0 请求队列 | `request.py`：sqlite 表 requests，`unprocessed→done/error/skipped`，key 为 canonical URL | 断点续跑、单条重试、分批流式（内存从 13GB 降到 246MB） |
| P1 限速 + robots | `politeness.py`：每域令牌桶（默认 2 req/s），403/429 自动减半，连续成功回速；robots.txt Disallow 直接 skipped | 不再把站点打到 403 |
| P2 抽取择优 | `extract.extract_best()`：trafilatura / JSON-LD / HTML 段落三候选打分取最高；粗抽只在剪枝 <40% 时兜底，html 通道再过列表页闸门 | 救回短页面，不再"抽不到就丢"，也不再让带导航的整页文字胜出 |
| P3 管道阶段 | `pipeline.py`：normalize → validate（阈值按通道：posts 120 / pages 60 / media 0）→ store | 丢弃原因可计数（`reasons`），能解释自己的漏采率 |
| P4 配置校验 | `schema.py`：每通道声明必填/可选键、类型；`collect.py --validate` | 99 个站一次校验，0 errors / 15 warnings |

## 关键设计

1. **一个通道一个模块**：`channels/<name>.py` 暴露 `run(site, limit, log, fetcher, kind) -> dict`。
   新增规则 = 新增文件 + 在 `channels/__init__.py` 注册表里加一行，**不改动任何已有通道**。

2. **通道之间不共享状态**：URL 发现（`discover.py`）、正文抽取（`extract.py`）、字段映射（`mapping.py`）、落盘（`store.py`）都是无状态函数，任何通道可复用。

3. **配置驱动**：站点差异全部写在 `sources.yaml`，不再写 if/else：

```yaml
- domain: example.org
  channel: wp-api
  via: impersonate          # direct | impersonate | cloud | browser
  base: https://example.org

- domain: news.example.org
  channel: json-list
  api_list: /stories/page/{page}.json?category={category}&api=true
  api_categories: [news, opinion, sport]
  api_items: stories
  free_field: is_premium
  api_url: "{base}/category/{category_name}/{id}"
  premium_marker: 'id="impression-panel"'

- domain: public.example.org
  channel: html
  list_urls: [https://.../Actualites]
  link_pattern: "/Actualites/[^/\"]+$"
```

4. **通用开关**（所有通道可用）：
   - `via`: `direct` / `impersonate`（curl_cffi 过 TLS 指纹）/ `cloud`（Browserbase fetch）/ `browser`
   - `no_embed`: 关掉 WP 的 `_embed`（少数站会 406）
   - `url_filter` / `link_pattern`: URL 白名单正则
   - `sitemap` / `feed` / `list_url` / `list_urls` / `page_step` / `pages`: 发现入口
   - `api_*`: JSON 通道的地址、查询串、条目字段、分类前缀

## 加一个新通道的步骤

```bash
# 1. 新建模块
cat > collector/crawler/channels/my_api.py <<'PY'
from crawler.channels.base import collect_urls
def run(site, limit, log, fetcher, kind="posts"):
    urls = [...]            # 用 site 里的配置拼出来
    return collect_urls(site, urls, limit, log, fetcher, kind)
PY

# 2. 注册
# channels/__init__.py 里加 "my-api": my_api.run

# 3. 配置
# sources.yaml 里 channel: my-api
```

## 验证

```bash
uv run --with requests --with pyyaml --with trafilatura python3 collector/collect.py --site <domain> --limit 3
uv run --with requests --with pyyaml --with curl_cffi python3 collector/verify_coverage.py
```

## 本次重构未改变的行为

- CLI 参数不变（`--site/--all/--limit/--kind/--workers/--reset`）
- 数据文件布局不变（`data/<domain>/urls.txt`、`articles.jsonl`、`*cursor.json`）
- 兼容旧脚本：`collect.py` 仍 re-export `map_wp_post / map_wp_media / append_rows / load_known / Fetcher / extract_article`
- 已对 6 个通道各做冒烟测试（wp-api、sitemap、rss、html、json-api、json-list），条数与重构前一致

## 出口策略（2026-11-04 修订）

**ego 只用于「侦察」：找出接口、URL 形式、必需参数和 cookie。**

- 用 ego 打开站点，在 DevTools/Network 里看真实请求（URL 形式、query、header、cookie）。
- 拿到契约后，**一律回到 HTTP 脚本**（`requests` / `curl_cffi`）批量抓，8–16 线程。
- 只有在"每次请求都被挑战、cookie 又拿不回来"这种极端情况下，才用 `via: ego` 兜底。

原因：ego 翻页受浏览器渲染限制，约 7 条/秒；同样的接口用 HTTP 并发能到 40–50 条/秒，而且不占 CPU。

> 教训：Saipan 之前被判定为"Cloudflare 站必须上浏览器"，实际只是 REST 只接受
> `?rest_route=` 形式而配置里少了 `api: rest_route`。ego 里明明能看到这个 URL，
> 却没有据此改配置——**看到请求就要把它变成配置**。

## HTML 页面流水线（层 2 / 3，2026-11-09）

「渲染抓取 → 剪枝降噪 → 结构化提取」里的抓取层仍由 `web.Fetcher` 的出口负责
（`direct` / `impersonate` / `ego` / `cloud` / `browser`），后两层在 `crawler/htmlpage.py`：

```
extract_best(url, body, site)
  candidates(): trafilatura 剪枝 / JSON-LD / 整页粗抽（仅当剪枝结果 <40% 才参与）
  htmlpage.enrich():
    noise_gate  → 列表页/导航页判定（link_ratio>0.35 且 Markdown<1200 字）→ 丢弃
    schema      → 字段 → XPath，确定性覆盖（无模型、离线可测）
    llm_fields  → 可选：任意 OpenAI 兼容接口补空字段（无 key 时不参与）
```

配置键（`sources.yaml`，`schema.py` 已放开校验）：
`item_selector`（XPath 返回 href，替代 `link_pattern` 过滤）、`noise_gate`、`schema`、`llm_fields`、
`llm_override`。可选 LLM 用 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`，
并认 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`。

取舍：图上的 Scrapling / Crawl4AI / Instructor 均评估后未接入（依赖重量与运行形态不合），
