# 采集范围规则 · 全站详情页

生效：2026-11-12（用户确认，适用于 `collector/sources.yaml` 全部信源，不只入口窗口站）

## 1. 规则

每个信源采集**全站内容详情页**，不是只采 RSS 窗口、单栏目或新闻列表。

- 枚举路径：主导航菜单 → 各栏目/组件列表（分类、featured、blog、archive）→ 翻页到底 → 详情页。
- 有 sitemap / REST / 接口的，与菜单枚举取并集，去重。
- 列表页本身不入库；只入库它指向的详情页。
- 详情页 = 同 host（或信源清单明确指定的子域）上由内容组件产出的页面：文章、新闻、页面、报告、出版物、公告、文档条目。

## 2. 各通道的"全量"口径

| 通道 | 枚举方式 | 说明 |
|---|---|---|
| wp-api | `/wp-json/wp/v2/types` 的所有 public 类型 + media | 不只 posts/pages；自定义 post type 也要枚举 |
| sitemap | sitemap 全部 `<loc>`，不做栏目过滤 | 显式 `url_filter`/`link_pattern` 只在"非本地主体"例外时保留 |
| html | 首页菜单 → 每个栏目列表 → 分页到底 → 详情页 | 用 `collector/survey_site.py` 生成候选配置后人工确认 |
| rss | feed 只作发现入口，必须找到栏目列表页继续翻页 | 不能只采 feed 里的 10 条 |
| json-list / json-api | 列表/接口全量翻页 | 付费墙/接口截断需在交付说明标注 |

## 3. 排除项（不算详情页）

- 搜索、标签、作者、日期归档、日历、分页页
- 登录/注册/表单/联系页
- 纯资源文件（jpg/png/css/js/pdf/zip/mp4）；文件 URL 可记录，但不抽正文
- 外链子站（`*.gov.vu` 等）：除非单列为新信源，否则不跟随
- 后台路径（`/administrator/`、`/wp-admin/`、`/login`）

## 4. 验收

- 菜单栏目逐项核对：每个栏目都能对应到已采详情页或"无详情页"的说明。
- 有独立总量的（X-WP-Total / sitemap `<loc>` / 接口 meta）：≥95% 且缺量可解释。
- 无独立总量的：标"入口窗口"，不冒充全量。
- 交付说明里逐站写清"范围 + 口径 + 例外"。

## 5. 非本地主体（不采集）

以下信源整站停采，不设栏目边界：

- `www.lefigaro.fr`（法国全国报，且 robots/403）
- `www.croix-rouge.fr`（法国红十字会）
- `www.fisheries.noaa.gov`（美国 NOAA）
- `www.spc.int`（区域组织 SPC）

`la1ere.francetvinfo.fr` 按本地版处理（Polynésie / Wallis-et-Futuna），保留，只采本地栏目。

## 6. 短正文（2026-11-12 调整）

不再按正文长度丢弃条目。正文为空/过短时保留元数据行（url、title、date、author、host 等），
正文列留空即可；已入库的历史缺口（tntv 3,898、fbcnews 等）在下次重跑时补齐。
