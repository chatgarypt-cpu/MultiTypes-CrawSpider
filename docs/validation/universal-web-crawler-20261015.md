# 通用采集 v0.1.0 验证记录

日期：2026-10-15。当前仅交付纯 Markdown skill；用户取消创建 Agent。运行验证使用本机现有工具，没有将检查程序放进 skill。

## 已完成

- skill-creator 的 quick_validate 通过，frontmatter 与命名有效。
- ZIP 仅包含 Markdown，SKILL.md 位于第一层技能目录内；相对文档引用可解析，包内内容与源码一致。
- Evose AI CMO 空间导入成功，详情显示“已上线、未被引用、4 个辅助文件”；文件树包含 SKILL.md、prompt.md 与 references。平台版本 1.0.0，包内 metadata.version 为 0.1.0。导入不等于模型运行验收。
- 按新流程在 [Books to Scrape](https://books.toscrape.com/) 进行只读跨页测试。该站说明自己是采集演示站，价格/评分是示例数据。
- 本机 Codex Firecrawl connector 分别读取首个列表页与其响应中发现的 next URL：两次请求都要求 maxAge=0、实际源站状态 200。
- Ego Browser 从列表 DOM 读取标题属性、价格和商品链接，两页各 20 条。独立核对 40 条的三个字段均对应；取前 30 条，30 个唯一商品 URL，无标题/价格/链接缺失。结果状态 target-reached，不声称网站全量完成。

## 结果与证据

- [30 条 JSON](../../artifacts/universal-web-crawler/20261015-books/books-30.json)
- [30 条 CSV](../../artifacts/universal-web-crawler/20261015-books/books-30.csv)
- [数量与核对记录](../../artifacts/universal-web-crawler/20261015-books/summary.json)
- 两页完整 Firecrawl 响应与两页浏览器 DOM 摘录保留在同一 artifacts 目录，用于复核；这些测试数据不进入 skill ZIP。

## 限制

该测试由本会话按流程执行，证明静态列表、观察到的 next、完整标题提取和字段核对可行。不是 Evose 模型执行，也不是另一个 Agent 的独立提示词评测；不能证明全部模型会遵循流程。

尚未验证 Evose 方法 schema、运行时技能注入、工具绑定与 Trace；内部 API 复用、动态交互、异步作业结果分页、限流/失败恢复也尚未实际测试。用户要求只交付 skill，因此不创建 Agent 做运行测试。验收场景已写入 references/acceptance.md，未来仅依据真实 Trace 更新通过项。

旧 RT 新闻样本与 REA Evidence 保留在历史目录，不能替代本次平台验收。
