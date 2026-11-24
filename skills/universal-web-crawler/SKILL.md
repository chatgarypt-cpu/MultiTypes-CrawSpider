---
name: universal-web-crawler
description: 使用宿主已挂载的网页、数据源和浏览器工具进行通用网页采集，发现可靠来源、验证字段与分页并交付可核对的数据。用于新闻、商品、职位、文档或其他网站的数据提取与爬取；纯 Markdown，无安装脚本，不要求 REA。不用于普通事实问答或执行外部写操作。
metadata:
  version: "0.1.0"
---

# 通用网页采集

读取 [prompt.md](prompt.md) 执行核心流程。Evose 将该文件注入 Agent 时直接使用已注入内容，不重复要求读取本地文件。

本技能仅包含 Markdown。它通过宿主已经提供并授权的工具获取数据，不自行安装包、启动 CLI/MCP、执行附带脚本或配置新服务；不假设平台有 REA。

工具调用按宿主的真实方法定义进行。Evose 市场选型与降级路线见 [工具接入](references/tool-routing.md)。核心规则全部包含在 prompt.md；Evose 仅注入该文件也能执行，不要求运行时能访问 references。

方法来源、参考方案与取舍见 [来源记录](references/sources.md)。

维护者用 [验收场景](references/acceptance.md) 检查实际工具表现。文本发布版本不代表所有站点或所有平台路线已通过实测。
