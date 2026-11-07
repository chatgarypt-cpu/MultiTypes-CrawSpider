# 方法来源与取舍

检索日期：2026-10-14 至 2026-10-15。以下是本次需求的最佳适配建议，没有独立基准支持“全行业最佳”排名。

## 主参考：官方 skills 与采集实践

[Firecrawl 官方 skill 集合](https://github.com/firecrawl/skills) 的 core 入口按已知 URL、链接发现、批量栏目和交互任务选择能力，并要求检查输出和来源。采用最小能力路由与已取得内容复用；不复制其 CLI 安装/鉴权流程，也不假设 Evose 暴露完整服务。

[Firecrawl Crawl 文档](https://docs.firecrawl.dev/features/crawl)：采用显式范围和页面上限、作业状态与结果读取分离、结果分页及源站错误检查。[Map 文档](https://docs.firecrawl.dev/features/map)：URL 发现存在覆盖和时效局限，因此不将映射视为全量。[Scrape 文档](https://docs.firecrawl.dev/features/scrape)：缓存策略与页面当前状态需要分别核查，采集时间不能证明源内容新鲜。

[Crawlee 请求队列](https://crawlee.dev/js/api/core/class/RequestQueue) 使用稳定请求身份，区分待处理与已处理；[BasicCrawler 配置](https://crawlee.dev/js/api/basic-crawler/interface/BasicCrawlerOptions) 提供请求上限、重试及失败处理。这里采用有界采集、去重和显式失败状态，不引入 Crawlee 依赖。skill 的 20 条/30 页/60 调用默认值与额外两次重试是本项目选择，不是上游默认值。

[Apify 作业与结果](https://docs.apify.com/academy/api/run-actor-and-retrieve-data-via-api) 区分运行、等待和取回输出；[Dataset API](https://docs.apify.com/api/v2/dataset-items-get) 提供分页读取。采用真实 run/dataset 身份与分页核对，避免把“启动 Actor”当作“取得数据”。

[Browserbase 官方 Fetch skill](https://github.com/browserbase/skills/blob/main/skills/fetch/SKILL.md) 区分简单请求与需要浏览器执行的任务；[Browser-to-API](https://github.com/browserbase/skills/blob/main/skills/browser-to-api/SKILL.md) 依赖捕获记录和实际响应体。仅采用按能力升级、观察与推断分开的方法，不附带浏览器/Node 脚本，也不将市场会话管理卡片当成完整浏览器 MCP。

## 补充流程参考

[Universal Data Acquisition Pipeline](https://github.com/Pranjay-kumar/universal-data-acquisition-pipeline-skill)，MIT，核查 revision `4bd33d5a12da55a0163295a2f6d68d63f2b7b84b`。

采用来源选择、小样本与覆盖验证的思路。它的核心是 Markdown，但浏览器辅助部分包含 Playwright/Patchright，不能把整个仓库称为零依赖；未将这个较新的社区项目作为成熟度证据。

本 skill 将方法适配为宿主工具执行，未复制辅助程序、完整子 skill 树或其固定的 15 段输出与重复审批流程。已有用户授权的采集范围可继续执行。

## 静态抓取候选的实测

[bestdeejay-design/web-scraper](https://github.com/bestdeejay-design/agent-skills/tree/92ecd773701d3ae63aac841600765f0d63e4feb8/skills/web-scraper)，MIT。其 scrape.py 确实只用 Python 标准库，但不是纯 Markdown 交付。

离线样例发现根元素链接缺失、纯 class 选择器错误合并记录、robots 通配规则处理不完整。源码快照和检查结果在本项目 `docs/research/zero-dependency-crawler-20261014/`；因此只作为对比材料，没有打包进纯 Markdown skill。

[Universal Scraping Architect](https://github.com/alirezarezvani/claude-skills/tree/main/engineering/universal-scraping-architect/skills/universal-scraping-architect) 的说明明确列出 Firecrawl/requests、BeautifulSoup/pandas 等依赖，仅验证脚本为标准库；不符合直接采用为零依赖方案的条件。

## Evose 接入依据

- [技能文档](https://evose.ai/docs/zh-CN/use/build/capabilities/skills/)：目录加 SKILL.md，ZIP 导入；Agent 勾选技能时 prompt.md 被注入。
- [工具文档](https://evose.ai/docs/zh-CN/use/build/capabilities/tools/)：工具来源为 MCP、HTTP 插件或 EvoTool 市场，实际方法需在平台配置中确认。
- [Agent 文档](https://evose.ai/docs/zh-CN/use/build/apps/agent/)：提示词 Agent 可绑定工具和技能，并通过调试 Trace 查看真实调用。

用户工具目录已收到，平台市场卡片已只读核查；具体方法与绑定/调试仍需实际执行。纯 Markdown 资产不自动安装或绑定这些服务。
