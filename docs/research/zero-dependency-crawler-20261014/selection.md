# 纯 Markdown 爬虫 skill 选型

用户约束：2026-10-14 确认为纯 Markdown；Evose 没有接入 REA。2026-10-15 已收到市场目录并查看市场卡片，具体方法/平台绑定尚未验证。

## 结论

初期筛选了 [Universal Data Acquisition Pipeline](https://github.com/Pranjay-kumar/universal-data-acquisition-pipeline-skill) 的 Markdown 方法。补充官方资料后，主要参考更新为 Firecrawl 官方 skills 与 Crawlee/Apify 执行规则；该社区方案留作补充。[最新改造依据](../industry-practices-20261015.md)。没有独立基准支持“全行业最佳”的声明。

没有找到可直接宣称“纯 Markdown 自带执行引擎、无需任何宿主工具、通吃网站”的成熟方案。Markdown 提供决策与流程；网页访问、JS 渲染和持久输出仍由宿主能力实现。

| 方案 | 实际依赖/资产 | 本次选择 |
|---|---|---|
| Universal Data Acquisition Pipeline | 核心 Markdown；浏览器辅助依赖 Playwright/Patchright | 采用来源选择和验证方法，移除运行程序要求与重型流程 |
| bestdeejay web-scraper | Markdown + 标准库 Python 脚本 | 非纯 Markdown；离线发现功能缺陷，不直接采用 |
| Universal Scraping Architect | Firecrawl、requests、BeautifulSoup、pandas 等 | 不直接采用 |
| Browserbase browser-to-api | 标准库 Node 脚本，但依赖上游抓包/浏览器执行 | 留在已有本地 REA 版本，不纳入本次纯 Markdown 交付 |
| 当前本地 web-reverse-crawl v0.1 | Python/Node 脚本 + 可选 REA | 保留已有验证成果；新 Evose 版本单独存放 |

## 纯 Markdown 版本

- `skills/universal-web-crawler/`：仅 Markdown，包含入口、可供 Evose 注入的 prompt.md 和参考资料。
- `agents/universal-web-crawler.md`：Evose 提示词 Agent 角色文本。
- 用户工具清单与市场卡片已映射为接入要求；Agent 创建、技能导入与平台采集仍待目标空间和真实方法验证。

## 实测证据

本目录 `bestdeejay-web-scraper/` 固定上游 revision `92ecd773701d3ae63aac841600765f0d63e4feb8`，保留 MIT 许可及来源 hash。`upstream-smoke.json` 是离线检查：a.item 漏提取根链接、.item 合并到后续 Box 文本、两个 robots 通配样例错误放行。

旧 RT 测试只证明已有本地流程通过，不将它冒充新的 Evose Agent 平台测试。后续需要至少验证公开 feed、静态列表、分页接口、动态渲染和错误/部分结果的实际工具行为。
