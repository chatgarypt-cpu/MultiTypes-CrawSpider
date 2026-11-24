# HTML 站采集：现状评估与三层流水线（2026-11-09）

图上的三层模型「渲染抓取 → 剪枝降噪 → 结构化提取」在 collector 里的现状、实测缺口、
开源库取舍，以及本次落地的最小实现。所有"实测"均在 2026-11-09 本机执行。

## 结论

- **层 1（抓取/渲染）已存在**：`direct` / `curl_cffi impersonate` / `ego` 本地浏览器 /
  Browserbase（cloud、browser）。缺的不是抓取方式，是"哪站该用哪种"。实测 8 个 0 行的
  html 站里，没有一个是"必须 JS 渲染才能拿到列表"的——是发现规则缺失、列表页被当文章、
  站点已死、或者按出口 IP 反爬。
- **层 2（剪枝降噪）有真实缺陷**：`extract.candidates()` 按文本长度择优，而"整页可见文字"
  一定比 trafilatura 剪枝结果长，于是**粗抽永远胜出**——已入库的 rss/html 站正文常以
  `Skip to main content / EXPLORE / Careers` 开头。本次改为剪枝结果优先、粗抽仅在剪枝
  明显失败（<40%）时兜底，并加列表页闸门。
- **层 3（结构化提取）缺失**：只有 meta / JSON-LD 启发式。本次加两个低成本实现：
  每站 `schema:`（字段 → XPath，无模型、离线可测）+ 可选 `llm_fields:`（任意 OpenAI
  兼容接口，未配置 key 时完全不参与）。
- **不引入新运行时依赖**：剪枝用已装的 trafilatura（`output_format="markdown"`），
  XPath 用已装的 lxml，LLM 调用用已装的 httpx。Scrapling / Crawl4AI / Instructor 均评估后
  暂不采用（理由见下表），需要渲染或多 provider 校验时再上。

## 实测缺口（14 个 `channel: html` 站）

| 站点 | 实测 | 根因 |
|---|---|---|
| www.gov.nu | 200 | 只有 base，无列表规则；发现到 5 条导航页并当成文章入库 |
| www.upng.ac.pg | 200 | 无列表规则；兜底正则把 `/media/*.css` 当文章 |
| dimenc.gouv.nc | 200 | 配置可用，28 条；正文择优带了导航 |
| www.wallis-et-futuna.gouv.fr | 200（高频请求后 ENHANCE_YOUR_CALM） | 配置可用，但列表页里大量栏目页被当文章 |
| la1ere.francetvinfo.fr | 200 | 配置可用（156 行）；正文择优带导航 |
| www.fwrm.org.fj | 200 | 配置可用（345 行） |
| minapacific.org | 200 | 老 ASP 站，文章链接是 `javascript:void(0)`，静态 HTML 拿不到详情页 |
| www.fbc.com.fj | 200 | 只是跳转 hub（3.7KB）；真实新闻在 `www.fbcnews.com.fj`（WP REST `X-WP-Total=71915`） |
| www.tokelau.org.nz | 403 | CloudFront 按出口 IP 拦截；本机 ego 浏览器同样 403 |
| www.lefigaro.fr | 403 | Datadome/付费墙，浏览器同样 Access Denied |
| www.belaunationalmuseum.net | 200（`cgi-sys/suspendedpage.cgi`） | 主机商已挂起，站点下线 |
| www.redcross.org.pg | 404 + 证书域名不匹配 | 站点迁移/下线 |
| www.wibdi.ws | 200（2.9KB 登录页） | 内容已下线 |
| www.tonga-broadcasting.net | 证书过期 + 302 `/login/` | 内容已下线 |

另有 4 个非 html 站把出口写成 `via: browser` / `via: cloud`，而 Browserbase 免费额度已耗尽
（`POST /v1/sessions` 返回 402 `Free plan browser minutes limit reached`），这些站点实际无法采集；
实测直连均 200，已改 `direct`。

## 开源库取舍（版本均为 2026-11-09 PyPI/GitHub 实测）

| 候选 | 版本/日期 | 结论 | 理由 |
|---|---|---|---|
| trafilatura | 2.3.1 / 2026-10-06 | **采用**（已装） | `output_format="markdown"` 官方支持 py3.14/3.15；剪枝与 Markdown 一步到位 |
| lxml | 6.1.3（trafilatura 依赖） | **采用** | 无新增依赖，XPath 做每站 schema |
| httpx | 0.28.1（venv 已装） | **采用** | 可选 LLM 层只做一次 JSON POST，够用 |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | 0.5.1 / 2026-11-04 | 暂不采用 | 纯抓取/渲染层（StealthyFetcher 基于 Playwright/Patchright）；27 个包、需 `scrapling install` 装浏览器。实测本机 ego 浏览器也被 CloudFront/Akamai 挡 → 这两站不是指纹问题，是 IP 问题；其余站不需要渲染 |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | 0.9.5 / 2026-10-14 | 不采用 | 95 个依赖、async-only、固定 litellm，与"无框架、单机脚本"的维护方式冲突 |
| [Firecrawl](https://github.com/firecrawl/firecrawl) 自托管 | — | 不采用 | compose 起 8 个服务（playwright/redis/rabbitmq/foundationdb…），不是本项目的运行形态 |
| [Instructor](https://github.com/567-labs/instructor) | 1.17.0 / 2026-09-09 | 暂不采用 | 需 41 个包；本项目可选层用 httpx + `response_format={"type":"json_object"}` 即可。注意 [DeepSeek 只支持 json_object](https://api-docs.deepseek.com/guides/json_mode)，不支持 strict json_schema |
| [pydantic](https://pypi.org/project/pydantic/) | 2.14.3 / 2026-11-05 | 暂不采用 | 字段校验目前用 31 列 schema + `pipeline.validate` 覆盖；引入 pydantic 只为一个可选层收益太小 |
| [readability-lxml](https://github.com/buriy/python-readability) / [markdownify](https://pypi.org/project/markdownify/) | 0.9 / 1.2.3 | 不采用 | trafilatura 已同时覆盖"读正文"和"出 Markdown"，且 py3.14 官方支持 |
| [parsel](https://pypi.org/project/parsel/) | 1.12.1 | 不采用 | CSS 选择器很轻，但 lxml XPath 已够用，不必再加一层 |

## 本次落地

| 文件 | 改动 |
|---|---|
| `collector/crawler/htmlpage.py`（新） | 剪枝 → Markdown；`link_ratio` 列表页闸门；`schema` XPath 提取；可选 LLM 补空字段（`LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`，也认 `OPENAI_API_KEY`/`DEEPSEEK_API_KEY`） |
| `collector/crawler/extract.py` | 择优修复：粗抽只在剪枝结果 <40% 时参与；`extract_best` 末尾走 `htmlpage.enrich` |
| `collector/crawler/discover.py` | `html_urls`：`urljoin` 解析相对链接、过滤资源/锚点、支持 `item_selector`（XPath 返回 href）、列表页地址生成抽成 `_page_urls` |
| `collector/crawler/schema.py` | 放开 `schema` / `llm_fields` / `llm_override` / `noise_gate` / `item_selector` 配置键与类型 |
| `collector/sources.yaml` | gov.nu / upng 补列表规则（含 `schema`、`noise_gate` 示例）；fbc 指向 fbcnews WP REST；4 个站出口改 `direct`；死站/反爬站写 `note` |
| `collector/tests/test_htmlpage.py`（新） | 10 条离线测试：剪枝择优、闸门、XPath schema、LLM 开关/坏 JSON、html_urls 相对链接与资源过滤 |

新增配置示例（`sources.yaml`）：

```yaml
- domain: www.gov.nu
  channel: html
  base: https://www.gov.nu
  list_urls: [/media-releases, "/media-releases/page:{page}"]
  pages: 20
  link_pattern: '/media-releases/(?!page:)[^/]+$'
  noise_gate: true                 # 层 2：列表页/导航页不进库
  schema:                          # 层 3（确定性）：字段 → XPath
    title: "//h1/text()"
  # llm_fields: [created_at, user_author_name]   # 层 3（可选）：配置 key 后补空字段
```

## 验证

- `check-site www.gov.nu`：5 → **138** 条候选（修复前是把 `/media-releases`、`/media-resources`
  等导航页当文章；现在全是 `/media-releases/<slug>`，分页 `page:2..` 不混入）。
- `check-site www.upng.ac.pg`：10（含 CSS）→ **34** 条公告。
- 临时目录端到端跑 gov.nu：3 行正文从「含导航的 2124 字」变为「1671 字干净正文」，
  `schema` 生效后标题从 `The Government of Niue | …` 变为 `Niue and Papua New Guinea Sign …`。
- `pytest`：新增 10 条全绿；全量套件仍只有既有的 4 条 `test_status.py` 失败
  （本机沙箱禁止 `ps`，与本次改动无关）。
- 回归：抽 sitemap（nbc）与 rss（palauconservation）各跑 2 条，正常入库、正文无导航前缀。

## 未做 / 风险

- **已入库约 91 万行不受影响**：旧择优逻辑产生的正文（rss/html 通道，含导航）仍在盘上。
  新逻辑只作用于后续采集；如需清洗历史数据要单独做迁移（会改 `data/`，本次未动）。
- **层 1 的本地渲染未接**：目前没有"必须渲染、且渲染能过"的站点。若以后出现纯 JS 站点，
  再引入 Scrapling `StealthyFetcher`（27 包 + `scrapling install`），上限与接入点已记录。
- **Browserbase 402**：`via: browser/cloud` 已无出口。要么充值，要么换本地渲染；
  本次只把能直连的站改 `direct`，`tokelau`/`lefigaro` 保持被挡状态并写 `note`。
- **死站/需人工确认**：belaunationalmuseum、redcross.org.pg、wibdi、tonga-broadcasting、
  minapacific（ASP + JS 链接）。建议在下一次交付前确认新域名，避免队列反复重试。

## 验收补充（2026-11-09，审查项 2 之后）

### UPNG：动态分页与 no_text 的真因

- 分页：`/announcements` 的 pager 为 `start=0,6,12,18,24,30`（共 6 页、约 36 条公告）。原配置
  `pages: 60` 过度；缩到 12 页仍覆盖全部（offset 0..66 ⊇ 0..30），现改为 **6 页**。
- **13 条 `no_text` 不是抓取失败，是闸门误杀**：UPNG 文章页正文短（Markdown ≈894 字）且导航
  链接多（`link_ratio≈0.57`），默认 `noise_gate` 阈值 0.35 把真文章当列表页丢掉；而列表页
  `link_ratio≈0.73`。修复：`noise_gate` 支持**按站数字阈值**（`true/false` 仍表示开关），
  UPNG 设 `noise_gate: 0.65`。
- 重跑结果：24 → **35 行**（+11 篇真公告），剩 2 条 `no_text` 是分类页 `221-academic` 和一条
  无正文公告，属预期丢弃；游标 `complete` 且 `reasons.no_text=2` 可解释。
- 教训：**"游标 complete" 之前先看 `reasons`**；`no_text` 既可能是列表过滤，也可能是抽取/
  闸门误杀，需要抽样核对。

### gov.nu：XPath 选择器复用验证

- `schema.title: //h1/text()` 在 8/8 篇文章上与 h1 完全一致；站点 `<title>`/`og:title` 带
  `The Government of Niue |` 前缀，所以 schema 覆盖是必要的。
- 日期不需要 schema：`extract_best` 已能从 meta/JSON-LD 取到 `created_at`（6/6 样本有值）。
- 抽样验收命令（新增，只读、不写库）：

```bash
uv run python collector/collect.py --check-site www.gov.nu          # 发现 138 条
uv run python collector/collect.py --check-extract www.gov.nu --samples 6
# → checked 6 ok 6，schema_ok=True，date/title 均有值
```

### Agent 生成站点规则的落地流程（采纳建议）

1. `--site-url <入口> --detect` 生成候选规则（或由 Agent 读站点 DOM 生成 `list_urls/link_pattern/
   item_selector/schema`）；
2. `--check-site <domain>` 看发现条数是否合理、分页是否翻完；
3. `--check-extract <domain> --samples N` 验证正文/标题/日期/schema 在**未参与生成**的样本上稳定；
4. 通过后写入 `sources.yaml`，小批量 `--limit 5` 实跑，再交给 supervisor；
5. 用 `verify_coverage` 的同范围口径证明采齐（缺口写进 `missing`）。
