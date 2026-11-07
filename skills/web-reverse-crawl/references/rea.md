# REA 接入

按当前连接的 REA 工具 schema 调用。以下示例按 6.4.0 的实际输入整理；版本变化时以实时工具定义为准。完整 REA 工作流需要时读取已安装的 `reverse-engineer-anything` skill，不复制其全部工具手册。

## 主动场景与响应内容

`capture_browser_scenario` 可启动已有 Chrome/Chromium 的临时隔离会话，或连接一个已知 loopback CDP target。已有用户浏览器需要登录态时才选 connect；不要为了探索接口复制个人浏览器 profile。launch 的进程与 profile 由 REA 管理。

局部演示例子（替换真实路径、目标和经页面确认的唯一 selector）：

```json
{
  "browser": {
    "mode": "launch",
    "executable_path": "/absolute/path/to/chrome",
    "headless": true
  },
  "start_url": {"url": "http://127.0.0.1:8765/", "query": []},
  "actions": [
    {
      "step_id": "initial_data",
      "action": "wait_for",
      "locator": {"kind": "css", "selector": "#list p:first-child"},
      "state": "visible",
      "timeout_ms": 10000
    },
    {
      "step_id": "next_page",
      "action": "click",
      "locator": {"kind": "css", "selector": "#next"},
      "timeout_ms": 10000
    },
    {"step_id": "settle", "action": "wait_for_timeout", "duration_ms": 250}
  ],
  "capture": {
    "events": ["network"],
    "at_end": ["url"],
    "after_each_step": [],
    "network": {
      "request_body": true,
      "response_body": true,
      "header_values": false
    }
  }
}
```

最后的短等待仅用于可控演示；真实网站优先等待确切的内容/状态，设置动作超时。`wait_for` 与点击的 locator 必须唯一。URL query 按 schema 的 `query` 项声明；secret 使用环境变量引用，不内联真实值。

`capture.network` 的 request_body、response_body、header_values 独立选择，默认均为 false。响应体需要明确选择，默认仅最终 URL 不足以分析接口。保存完整 Evidence JSON，保留 `evidence_id`、`normalized_result.completeness`、limitations 和事件序号。MCP 有时把 JSON 放在 text content 中，保存其中的 JSON 而不是整个工具调用包装。

网络内容可能是 `captured`、`not_requested`、`not_exposed` 或 `unavailable`。捕获的 body 用 base64、字节数和 SHA-256 表示；响应是浏览器解码后的字节，不是原始压缩传输字节。GET 的 request body 未暴露不等于证明没有 body。动作全部完成也不保证网络记录完整。

错误结果（包括 `cleanup_incomplete` 的 partial_observation）不作为成功 Evidence 输入。先记录错误，检查 provider 报告的资源；只处理可确认属于本次操作的资源，修复具体失败后再考虑重试。不要关闭用户已有浏览器。

## 被动观察

已有端点时用 `list_browser_targets({cdp_endpoint, allowed_origins})`，选择任务的准确 target_id。没有已知 CDP 端点就使用现有浏览器工具或 launch 路线，不扫描调试端口或访问其他 tabs 来猜目标。

- `inspect_web_page`：被动 DOM、脚本、资源和 attach-window 网络元数据；不会点击、导航或主动 eval 页面。
- `analyze_web_bundle`：在静态脚本中推导 chunk、路由和 endpoint 候选。脚本源、JSON shape、source map 等按当前 schema 选择。
- 上述被动工具不应被描述为保留完整请求/响应体，也不能回收接入前的所有网络活动。

过滤 origin 只是分析范围，不是网络防火墙。页面仍可能加载外部资源；跨域 API 需要按用户目标明确加入分析范围。

## 离线抓包与脚本追踪

- HAR：`inspect_web_network_capture({capture_path: "/absolute/file.har", format: "har"})`。按原始 ordinal 定位目标记录；它不会访问录制 URL，也不能证明现在还可请求。
- 已保存的 page/scenario Evidence：`export_web_scripts({capture_path, output_directory})`，output_directory 必须尚不存在，父目录存在。只导出已保留的脚本字节，不重新下载。
- 使用导出结果返回的 `analysis_input` 调用 `analyze_javascript_application`，大输入选 summary，再通过 `inspect_analysis_view` 查模块。
- 用 `trace_application_feature` 的确切 endpoint/route/module seed，或 `trace_javascript_semantics` 定位具体参数来源。保留静态推断与运行观察的区别。`recover_javascript_sources` 有额外工具/平台要求，不能当作默认跨平台解混淆能力。

没有可用 provider 时继续已有的浏览器/HAR路线，说明缺失证据。不要每次诊断都重装 REA 或升级 Node；配置健康与当前 MCP 连接是两件事。
