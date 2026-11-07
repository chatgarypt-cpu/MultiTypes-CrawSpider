# 行业实践与改造依据

核查时间：2026-10-15。当前目标仅交付纯 Markdown skill，用户已取消创建 Agent；无 REA、程序、SDK 或 CLI 要求。本文给出适配选择，不声称有公认的全行业最佳爬虫 skill。

## 选型

以 [Firecrawl 官方 skills](https://github.com/firecrawl/skills) 的能力路由作为主要参考，辅以 [Crawlee](https://crawlee.dev/js/api/basic-crawler/interface/BasicCrawlerOptions) 和 [Apify](https://docs.apify.com/academy/api/run-actor-and-retrieve-data-via-api) 的执行/结果规则。官方方案本身包含服务、CLI 或库配置，不能直接称为无运行依赖；本项目提炼流程并接到宿主真实工具。

先前的 Universal Data Acquisition Pipeline 保留作补充参考，详见 [初期选型](zero-dependency-crawler-20261014/selection.md)。不以新社区项目或仓库星数证明某项 skill 成熟。

| 具体改造 | 依据与理由 | 在本项目中的落实 |
|---|---|---|
| 按任务用最小工具 | [官方 core skill](https://github.com/firecrawl/skills/tree/main/skills/core/firecrawl) 按 URL、发现、批量、交互路由 | 已给 URL 直接抓；仅需交互时才升级；复用已取得内容 |
| 限定栏目、页面和调用预算 | [Crawl](https://docs.firecrawl.dev/features/crawl) 与 Crawlee 提供范围和数量边界 | 设置显式上限，防止无限筛选/日历循环；条数与页数分开 |
| 区分发现与覆盖 | [Map](https://docs.firecrawl.dev/features/map) 的链接发现并非完整、最新清单 | map、RSS、total 与目标完成分别判断 |
| 处理缓存与时效 | [Scrape](https://docs.firecrawl.dev/features/scrape) 有缓存和新鲜度语义 | 采集时间、源时间、缓存年龄区分，不能确认就标注 |
| 身份与分页稳定 | [RequestQueue](https://crawlee.dev/js/api/core/class/RequestQueue) 区分请求身份/处理状态 | ID/GUID/canonical 去重，续页保留过滤条件，重复游标报告异常 |
| 有界重试与失败记录 | Crawlee 的请求上限、重试、失败回调 | 临时错误少量重试；认证、结构与额度分别处理；失败不伪装空结果 |
| 异步作业结果取全 | Apify 运行/读取分离；[Dataset 分页](https://docs.apify.com/api/v2/dataset-items-get) | 查询既有 job/run；作业终态后取回分页，区分服务商和站点状态 |
| 接口观察与推断分开 | [Browser-to-API](https://github.com/browserbase/skills/blob/main/skills/browser-to-api/SKILL.md) 要求捕获证据 | 未观测端点不编造；宿主无抓包能力不承诺逆向分支 |

## Evose 市场核查

依据用户目录与登录界面，只读查看 `AI CMO` 空间的市场搜索：Firecrawl 内置 6 个工具；Firecrawl 网页抓取 2 个工具；Browserbase 18 个工具，其说明是会话/context/项目/扩展/日志/调试 URL/下载元数据管理。未安装市场工具，也未读取方法 schema 或执行 Evose Trace。之后按用户指定空间导入了 skill，平台显示已上线；未创建 Agent。

结论：通用采集起步从两种 Firecrawl 卡片择一；Browserbase 暂不能作为已证实的交互执行层。工具包数量不说明方法覆盖。详细配置见 [工具接入](../../skills/universal-web-crawler/references/tool-routing.md)。

## 验证范围

静态检查验证包结构、frontmatter 与引用；独立的本机 Firecrawl + Ego Browser 测试验证静态列表两页和字段一致性，记录见 `docs/validation/universal-web-crawler-20261015.md`。该测试由本会话按新流程执行，并非 Evose 模型运行，也不是独立 Agent 的自动化评测。动态交互、内部 API、异步作业和失败恢复仍需对应实际方法与 Trace。
