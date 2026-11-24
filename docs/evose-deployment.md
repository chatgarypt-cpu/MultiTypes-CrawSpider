# Evose skill 交付记录

日期：2026-10-15。用户指定使用现有 AI CMO 空间，随后明确要求“不要创建 agent，只交付 skill”。

## 后续默认上传位置

用户随后指定使用 [Tiwork 内部研发测试空间](https://tiwork.lianyuinc.com/workspace/230522223179931648/build-service/agent)，工作空间 ID `230522223179931648`。后续本项目的 skill 上传默认进入该空间的技能页面，继续只交付 skill。

2026-10-15 已按用户要求上传相同 ZIP 到 Tiwork 的“AI CMO内部测试空间”。导入前按名称搜索，无匹配技能；导入时识别 1 个技能、5 个文件。详情页显示“已上线”“生效版本 1.0.0”“未被引用”“4 个辅助文件”，SKILL.md 中 metadata.version 仍为 0.1.0，文件树包含 prompt.md 与 references。未创建 Agent、未绑定应用、未执行采集测试。

[Tiwork 技能详情](https://tiwork.lianyuinc.com/workspace/230522223179931648/scalability/skill/236373345792905216/configure)

以下保留原 Evose 导入历史。

## 已完成

- ZIP 导入成功：平台识别 1 个技能、5 个文件，名称 universal-web-crawler，标签“网页采集”。
- 技能详情显示“已上线”“未被引用”“4 个辅助文件”。文件树可见 SKILL.md、prompt.md、references。
- 平台自动生成生效版本 **1.0.0**；导入包的 metadata.version 为 **0.1.0**。两者属于不同版本编号，未另行发布版本。
- 未创建 Agent、未绑定既有 Agent，也未安装市场工具。

[Evose 技能详情](https://app.evose.ai/workspace/194837538156867584/scalability/skill/236338745417166848/configure)

本地可分发包：`packages/universal-web-crawler-v0.1.0.zip`，SHA-256：`d178a094b50041eaecdfce394125ce525b4e780fb435d439bebdc11bd3166ffa`。仅含 5 个 Markdown 文件，无代码或 REA 要求。

平台页面证据保存在 `artifacts/universal-web-crawler/evose-skill-import-20261015.txt`。导入行为不等于运行时技能注入、工具方法或采集正确性测试。

## 验证与使用边界

本机已完成静态结构检查和跨页采集验证，详见 [验证记录](validation/universal-web-crawler-20261015.md)。Evose 未执行模型/工具 Trace 测试；当前交付范围不包含创建 Agent。

未来用户决定在既有应用使用该 skill 时，挂载所需的实际 URL 读取能力即可起步；方法与参数以真实定义为准。工具选型、能力缺失处理与验收场景已包含在 skill references 中。prompt.md 自包含核心规则，references 用于配置和维护。

之前的 Agent 角色草稿归档到 `docs/archive/universal-web-crawler-agent-draft.md`，不再作为本次交付物。
