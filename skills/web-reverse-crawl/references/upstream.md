# 上游来源与复用范围

- REA：由已有 `reverse-engineer-anything` skill 与 `rea` MCP 提供网络、浏览器和 JavaScript 证据；本 skill 不复制 REA、不修改它的配置、不捆绑原生分析引擎。当前接入示例按 npm 6.4.0 核对。源：https://github.com/morluto/rea
- Browserbase browser-to-api：`scripts/browserbase/` 包含未经修改的 11 个 Node 脚本和 MIT LICENSE.txt，固定到 `6811ca31163332d9d60309cff48e77f09de37a17`。每个文件的 SHA-256 在 `scripts/browserbase/upstream.json`。源：https://github.com/browserbase/skills/tree/6811ca31163332d9d60309cff48e77f09de37a17/skills/browser-to-api
- 本 skill 新增的代码仅为 REA/HAR → 上游 trace 输入的格式适配与检查；不重新实现浏览器、schema 推导或采集框架。

可选方案只在实际需要时复用，不作为必须全部安装的依赖：

- OpenCLI 的已有站点 adapter 或 adapter-author：适合已有站点命令、浏览器会话依赖与持续维护。https://github.com/jackwener/opencli
- Scrapling：现有抓取/解析/Spider 框架满足批量执行需求时使用；简单分页 HTTP 不必为此加依赖。https://github.com/D4Vinci/Scrapling
- BrowserAct Skill Forge：其探索与站点 skill 生成流程可作参考；本 skill 不继承它的外部审批、依赖安装或无限探索指令。https://github.com/browser-act/skills/tree/main/browser-act-skill-forge

更新上游时先核对实际输出格式与支持版本，再替换固定快照并运行适配器检查及一次真实小样本。保留许可和来源记录，不仅依据上游 README 宣传判断兼容性。
