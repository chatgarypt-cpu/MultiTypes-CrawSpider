# 残留站点的采集方案调研（2026-11-03）

针对当前剩下的 4 类站点，查了业内做法和开源项目。结论按 ponytail 口径写：只加必要的，不重造轮子。

## 一、Cloudflare / WAF 拦截（约 10 站）

| 方案 | 规模 | 状态 | 评价 |
|---|---|---|---|
| **FlareSolverr** | 16.1k★ | 活跃（2026-10-24 有提交） | 起一个本地 HTTP 代理，暴露 `/v1` 接口，内部跑 Chrome+Selenium 解挑战。缺点：并发建会话会漏浏览器进程、日志会打印 cookie 和代理凭据 |
| **Camoufox** | 13k★ | 活跃（2026-11-02） | 改造版 Firefox，用 fpgen 生成指纹（字体/语音/GPU/媒体设备），Playwright 直接换 `browser_type` 即可，无需 Docker |
| Solverr / Solvearr | 15–38★ | 小项目 | FlareSolverr 协议的 fork，不建议押注 |

**对我们的结论**：已经有 `ego-browser`（本地 Chromium），它在 islandtimes 上能过 CF，但 solomonstar 不行——差的是**指纹伪装**，不是浏览器本身。最省的做法是加 `camoufox` 一个依赖（`pip install camoufox`），用它跑那 3–10 个站；FlareSolverr 只有在"多进程共享一个解题服务"时才值得上 Docker。

## 二、HTML 站没有列表规则（约 20 站）

| 方案 | 说明 |
|---|---|
| **trafilatura 自带发现** | 已安装的 2.3.1 就有 `--sitemap` / `--feed` / `--crawl` / `--list`，能自动找 sitemap 和 feed，并用 Courlan 过滤导航链接 |
| **Courlan 1.4.0** | adbar 出的 URL 处理库：规范化、去重、过滤导航页/非文章页，就是"无规则找文章链接"的那一层 |
| **go-pkg-spider** | 206★，Go 写的中文新闻站通用抽取库（链接分类 + 新闻要素 + 正文），思路可借鉴，但语言不匹配，不直接引入 |

**对我们的结论**：不用写 20 份规则。先对每个站跑一遍 `trafilatura --sitemap --list`（很多 WordPress 站的 sitemap 路径我们没试全，比如 `/wp-sitemap.xml`、`/sitemap_index.xml`、`/news-sitemap.xml`），拿不到再退回 `courlan.extract_links` + 日期/路径模式过滤。这套本身就在现有依赖里，零新增。

## 三、WP 报的总数比能取到的多（7 站）

这是 WordPress 的已知行为：`X-WP-Total` 统计所有状态（含草稿、私有、被插件过滤的媒体），匿名 REST 请求只返回公开条目。社区有专门的问答记录这个现象（[WordPress StackExchange](https://wordpress.stackexchange.com/questions/408487/get-all-media-from-wp-json-wp-v2-media)）。可尝试 `?post_status=inherit` 或按 `parent` 逐篇取，但多数站无解——**这类缺口应该接受，不要为了凑数去绕**。

## 四、已失效/长期打不开的站

- **Common Crawl**：129 个索引，最新 `CC-MAIN-2026-43`（2026-10）；`index.commoncrawl.org/CC-MAIN-xxxx-index?url=<前缀>&output=json` 查归档记录，再从 S3 取 WARC。适合"站已死但内容有价值"的场景；通配查询很慢（实测 504），只适合精确 URL。
- **Wayback CDX**：同理，粒度更细。

**结论**：这 33 个零数据站里，真正值得去归档里捞的没几个（多数是政府小站、内容价值低），暂不做。

## 五、落地建议（按性价比）

1. **加一个依赖 `camoufox`**，专门跑 3 个 Cloudflare 站（islandtimes / saipan / solomonstar）和 3 个浏览器型 HTML 站；比升级 Browserbase（$20/月）便宜且可控。
2. **HTML 站用 trafilatura 的 sitemap/feed 发现**，写一个 `discover.py` 批量为 20 个站找列表，能自动的自动采，找不到的标"需人工"。
3. **WP 总数缺口**：接受，写进交付说明。
4. **Common Crawl**：只在用户点名要某个死站内容时再做。

## 参考

- [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)（16.1k★）
- [Camoufox](https://github.com/daijro/camoufox)（13k★）
- [go-pkg-spider](https://github.com/suosi-inc/go-pkg-spider)（206★，规则无关的新闻抽取思路）
- [trafilatura 用法文档](https://trafilatura.readthedocs.io/en/latest/usage-python.html)（`--sitemap` / `--feed` / `--crawl`）
- [Common Crawl 索引 API](https://commoncrawl.org/search?query=cdx)
- [WP media 总数问题](https://wordpress.stackexchange.com/questions/408487/get-all-media-from-wp-json-wp-v2-media)
