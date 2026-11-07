# REA_CrawlSpider

独立的网页采集项目。当前仅交付纯 Markdown 的 `universal-web-crawler` skill，由宿主现有工具执行，无附带程序、包安装或 REA 要求。原有本地 REA 版本保留。用户已取消创建 Agent。

## Evose 纯 Markdown 版本

- `skills/universal-web-crawler/`：v0.1.0，入口、可注入的 prompt.md、工具选择、来源与验收场景，全部为 Markdown。
- `docs/archive/universal-web-crawler-agent-draft.md`：取消 Agent 方案前的历史草稿，不属于当前交付。
- `docs/evose-deployment.md`：平台导入、挂载工具与实际验收说明。
- `docs/research/industry-practices-20261015.md`：官方最佳实践与具体改造依据。
- `packages/universal-web-crawler-v0.1.0.zip`：仅含上述 skill 的 Markdown。

主参考为 Firecrawl 官方 skills，结合 Crawlee 请求/失败处理和 Apify 作业/结果管理。平台卡片以实际方法为准：Firecrawl 两种卡片择一；有需要才加搜索或交互工具。市场中 Browserbase 卡片描述会话管理，不能直接保证页面交互能力。零依赖指技能资产纯 Markdown，模型与网页访问由平台提供。

已导入 Evose 的 AI CMO 空间，平台显示已上线，平台版本 1.0.0（包内文本版本 v0.1.0）。未创建 Agent、未绑定既有 Agent、未安装市场工具。部署与验证记录见 `docs/evose-deployment.md`；旧 RT 测试和本机 Firecrawl 验证不代表 Evose 运行实测。

## 已有本地 REA 成果

- `skills/web-reverse-crawl/`：skill v0.1.0 源码、REA 接入说明、接口推导适配器和检查脚本。
- `skills/web-reverse-crawl/scripts/browserbase/`：固定版本的 Browserbase 接口推导脚本，原样保留 MIT 许可及文件校验记录。
- `examples/rt/collect_rt.py`：已实测的 RT RSS 采集示例，只用 Python 标准库。
- `artifacts/rt-english/20261014-test/`：RT 测试记录、20 条新闻样本、浏览器核对与 REA Evidence。
- `artifacts/local-demo/20261014/`：本地演示站的 REA→接口推导→客户端采集验证记录。
- `packages/web-reverse-crawl-v0.1.0.zip`：可分发 skill 包。
- `DEV_SPEC_Web_Reverse_Crawler_Agent.md`、`web-reverse-crawler-agent-v0.1/`：原有设计草稿与早期可行性 demo，保留作为参考；实现范围以当前用户要求为准。

## 使用 skill

本机已安装的 `~/.codex/skills/web-reverse-crawl` 链接到本项目的 `skills/web-reverse-crawl/`。后续直接编辑项目中的源文件即可保持本机调用一致。

```text
使用 $web-reverse-crawl 分析 https://目标网站，采集标题、链接和发布时间，并生成可重复运行的采集脚本。
```

在其他机器上使用时，安装 `skills/web-reverse-crawl/` 或解压 ZIP 到对应 skill 目录；需要 REA 分析时按其官方 setup 配置 MCP。离线接口推导需 Python 3.10+ 和 Node 18+，无需额外 pip/npm 依赖；REA 本身使用它支持的 Node 版本与已有浏览器。

## 运行 RT 示例

在本项目根目录执行：

```bash
python3 examples/rt/collect_rt.py --feed all --limit 20 --out outputs/rt-first-run
python3 examples/rt/collect_rt.py --feed news --limit 20 --out outputs/rt-news-first-run
```

输出目录必须尚不存在。输出包括文章 JSON、CSV、RSS 来源快照和采集元数据。RSS 是最近 100 条的当前窗口，不代表历史全量；实际可用数量不足时标记 `feed-window-limited`。

## 本地检查

```bash
python3 skills/web-reverse-crawl/scripts/test_infer_api.py
```

当前 5 项检查覆盖 REA body 完整性、失败/错绑事件拒绝、origin 筛选、缺失 body、重复 JSON、输出不覆盖及 HAR→OpenAPI 链路。RT 已验证 20 条样本的标题和链接与浏览器 DOM 对应，并抽查两篇文章的发布时间。

真实私有 API 的接口推导、鉴权和历史分页尚未通过 RT RSS 测试；需使用适合这些分支的目标逐项验证。

## 源码与测试记录

`artifacts/` 和 `packages/` 是本机保留的测试/分发产物，默认不进入 Git。历史 Evidence 与报告保留原始调用路径，迁移映射记录在 `docs/relocation.json`。RT 测试目录中的 `collect_rt.py` 是当时执行的快照，日常维护使用 `examples/rt/collect_rt.py`。

## 采集器（collector/）

109 个南太平洋信源的批量采集，代码在 `collector/`，说明见 [collector/README.md](collector/README.md)。

- 运行数据在 `data/`（每站 jsonl、游标、日志、队列状态），交付 CSV 在 `output/csv/`；
- 看板 `python3 collector/serve.py` → http://127.0.0.1:8787/ （SSE 实时推送，可调并发 1–10）；
- 采集对象：文章、页面、媒体元数据；出口支持直连 / Browserbase 云抓取 / 浏览器隧道 / TLS 指纹；
- 策略与调研记录见 `collector/STRATEGY.md`、`docs/research/crawl-20261027/`。
