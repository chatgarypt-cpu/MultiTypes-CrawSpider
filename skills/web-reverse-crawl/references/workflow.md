# 接口推导与交付

## 离线接口推导

内置 `scripts/infer_api.py` 只把 REA scenario 或 HAR 1.2 转为 Browserbase 推导脚本接受的输入。需要 Python 3.10+、Node 18+；无 pip/npm 依赖安装步骤。脚本不重放请求，也不执行抓包中的 JavaScript。

先选定 capture 路径、工作区输出目录和目标数据 origin。用 skill 的真实绝对路径调用，例如：

```bash
python3 /absolute/skill/scripts/infer_api.py /absolute/rea-capture.json \
  --out /absolute/new-run --origin https://example.com \
  --origin https://api.example.com
```

同一命令支持 HAR。`--out` 必须是新目录；`--origin` 可重复，必须是精确 HTTP(S) origin，包括非默认端口。大于 64 MiB 的抓包先用现有工具缩小到相关记录，再保留原文件及切片关系。

原生 browser-trace 目录可以直接复用上游入口：

```bash
node /absolute/skill/scripts/browserbase/discover.mjs \
  --run /absolute/browser-trace-run --out /absolute/new-api-spec \
  --origins example.com,api.example.com
```

适配器保留原始文件 SHA-256、REA Evidence ID、原始 ordinal/transaction/event 引用、来源 completeness 和 omissions。中间请求 ID 是合成的配对键，不是真实 CDP requestId；不可据此宣称 CDP 时序/initiator 已观察。REA 内容先校验 base64、字节数、digest 和所属事件；HAR 没有 REA 的逐 body 身份保证。

只转入白名单 origin 且有有效响应状态的记录。缺失/二进制 body 不制造 schema，原始 body 状态留在 conversion.json。只传递 Content-Type，其他 headers 不进入推导产物；这是数据最小化，不能证明接口不需认证。原始 capture、URL query、请求体和响应体仍可能含敏感内容，留在本地受控目录；分发前检查并移除实际敏感内容，不声称自动脱敏完备。

## 产物含义

`<out>/conversion.json` 是格式转换与覆盖说明。

`<out>/api-spec/` 是上游脚本产出：

- `openapi.json` / `openapi.yaml`：从样本推导的接口与 schema；字段 required 只代表这些样本都存在。
- `confidence.json`、`report.md`、`samples/`：推断置信度、示例和覆盖信息。
- `index.html`：本地查看报告。里面的 curl/客户端示例不是自动获准执行的请求。
- `client.mjs`：上游只在其支持的 endpoint 形态下生成。检查实际存在性；生成不代表请求已经通过验证。

核对 `client.mjs` 的真实导出名再写调用代码；固定快照中报告示例的函数名可能与实际导出名不同。示例也可能把日期等普通字段误标成 `<redacted>`；字段验证回到原始捕获内容与真实页面，不把报告示例当作源数据。

上游会过滤 analytics、HTML、session 等噪声，路径模板与 GraphQL 拆分属于启发式。看 `intermediate/` 与 confidence，避免把误过滤、URL 折叠和遗漏 body 当作不存在。推导为空时回查来源/过滤，不臆造 endpoint。

## 最小站点交付

对“以后重复采集”的任务，交付站点专用脚本及一份简短 findings.md，包含：

1. 用户目标、目标 origin、字段/范围。
2. 选定数据源、已观察请求与实际回放证据、字段映射、会话依赖。
3. 分页参数/游标、结束条件、去重 key、请求间隔和上限。
4. 脚本运行命令、实测数量、最后状态：达到目标 / 上游结束 / 部分结果 / 失败。
5. Evidence ID 或原始抓包 ordinal、未确认的假设与限制。

生成脚本优先复用现有 HTTP 库，否则公开简单请求用标准库/原生 fetch。只对已经观察且实测的参数和字段编码。缺失字段、登录页、重复页/游标、上游错误与重定向到其他 origin 都不能当作正常结束。HTTP 200 加空数组也需要结合 API 语义验证。

先验证第一页及下一页/另一组输入，确认字段与网页一致；再按照用户目标采集。去重后的数量需单独报告。达到最大页数属于部分结果，除非已同时证实上游耗尽或已满足指定数量。瞬时读请求失败可以有少量有界重试，认证/结构变化则停下修复；未知写请求不自动重试。

只有用户请求生成可安装站点 skill 时，才把该脚本封装到 `SKILL.md + scripts`。复用现有 skill-creator 的检查器，实际跑小样本；把验证日期与所需浏览器/会话写入生成的站点交付，不把它描述成永久有效。
