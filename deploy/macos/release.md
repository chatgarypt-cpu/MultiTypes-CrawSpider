适用于 macOS 15 或更高版本的 Apple Silicon Mac（arm64）。DMG 包含工作台、Python 运行环境和默认采集依赖。打开 DMG，将应用拖到「应用程序」，双击后会在默认浏览器打开本机工作台。再次点击 Dock 图标可重新打开页面。

首次使用从 0 个信源开始。在工作台输入自己的网址，试采并核对样本后确认入队。数据保存在 `~/Library/Application Support/MultiTypes-CrawlSpider/`。应用仅监听本机，自动选择空闲端口。此包提供默认 HTTP 采集功能，可选的浏览器渲染引擎需另行配置。

本版使用 ad-hoc 签名，未经 Apple Developer ID 签名和公证。从 GitHub 下载后，Gatekeeper 可能阻止首次打开。请先核对下载来源和 SHA-256，再参考 [Apple 的首次打开说明](https://support.apple.com/zh-cn/102445)。本版跳过本机启动及试采验收。
