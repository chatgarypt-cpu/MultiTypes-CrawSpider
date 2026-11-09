# 采集入口说明（不用找我，自己就能跑）

三个入口，按需选：

## 1. CLI（最直接）

```bash
cd /Users/gary/项目开发/REA_CrawlSpider

# 依赖：仓库根 pyproject.toml + uv（首次 uv sync 建 .venv）
# 单篇文章
uv run python collector/collect.py --url "https://example.com/news/xxx.html"

# 整站：给它一个首页/列表页，自动识别用哪种规则（WordPress API / sitemap / RSS / HTML 列表）
uv run python collector/collect.py --site-url "https://example.com" --limit 100

# 只看它识别出了什么规则，不抓
... collector/collect.py --site-url "https://example.com" --detect

# 抓 sources.yaml 里已有的站
... collector/collect.py --site www.postcourier.com.pg --limit 500
... collector/collect.py --site www.postcourier.com.pg --kind media --limit 0

# 校验所有站配置
... collector/collect.py --validate
```

数据落在：
```
data/<域名>/articles.jsonl     # 每行一个 JSON
data/<域名>/urls.txt           # 已采 URL（去重）
data/<域名>/run.log            # 运行日志
output/csv/<域名>.csv          # 31 列目标格式
```

`--site-url` 识别的规则会记到 `data/adhoc_sites.json`，下次同域自动复用。

## 2. 网页表单（不碰命令行）

启动服务（只需一次）：

```bash
nohup uv run python collector/serve.py > data/serve.log 2>&1 &
```

然后打开：

| 地址 | 用途 |
|---|---|
| http://127.0.0.1:8787/crawl | **启动采集**：填 URL、选整站/单篇、填条数，点"开始" |
| http://127.0.0.1:8787/ | 采集看板：总览 / 正在运行 / 已结束，点行看该站 CSV |
| http://127.0.0.1:8787/api/jobs | 任务列表（JSON） |

表单提交后是**后台任务**，页面每 5 秒刷新状态，每行有 log 链接。

## 3. HTTP API（脚本/其它系统调用）

```bash
# 整站（自动识别规则，最多 100 条）
curl -X POST http://127.0.0.1:8787/api/crawl \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","mode":"site","limit":100}'

# 单篇
curl -X POST http://127.0.0.1:8787/api/crawl \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/news/1.html","mode":"article"}'

# 看任务
curl http://127.0.0.1:8787/api/jobs
```

返回 `{"ok":true,"job":{"id":"76df9b2f",...}}`，`data/jobs/<id>.log` 是它的输出。

## 自动识别顺序

`--site-url` / API 的 `mode: site` 会按这个顺序探测：

1. WordPress REST（`/wp-json/wp/v2/posts` 或 `?rest_route=`）→ 能拿到精确总数
2. sitemap（`/sitemap.xml`、`/sitemap_index.xml`、`/wp-sitemap.xml`）
3. RSS / Atom（`/feed/`、`/rss.xml`、`/atom.xml`、Joomla feed）
4. 都不中 → 把给的 URL 当列表页，用链接规律启发式抓

遇到 Cloudflare（403 + `Just a moment`）会自动换 `curl_cffi` 的 Chrome 指纹重试一次，成功则把 `via` 记成 `impersonate`。

## 注意

- 网页/API 入口依赖 `collector/serve.py` 在跑；CLI 不需要。
- 队列是持久的（`data/state.db` 的 `requests` 表），中断后重跑会接着来，不会重复抓。
- 每域自适应限速（默认 2 req/s，被 403 会自动降速）；robots.txt 里 Disallow 的路径直接跳过。
