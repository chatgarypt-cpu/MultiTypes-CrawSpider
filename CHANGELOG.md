# 更新日志

## 2026-11-24 · 未发布

- 网页任务遇到接口缺失或采集不完整时，会沿公开栏目和分页查找详情。没有可见链接和正文的动态页面可用 Chromium 渲染。已识别的首页、栏目和分页只用于发现链接，正文和元数据提取均跳过这些入口页。
- 自动发现网页声明的同域 RSS / Atom 地址。订阅和栏目分页发现的同一篇文章只保存一次。
- LNC 支持定向重试旧 HTTP 400，并用 Trafilatura 清理正文中的打印入口、账号提示和推荐区。TNTV 支持一次历史补漏，中断后按断点续跑。
- Samoa 的失败详情进入持久队列，落盘后才标记完成。分别记录列表付费标志和详情访问结果。配置中的重复键会在命令行启动时报告。
- MECDM 支持保存 PDF 原件并提取正文和元数据。重复原件按内容去重，解析失败保留在重试队列。
- 新增逆向采集：`channel: archive`（Wayback CDX 发现历史页面）+ `via: archive`（正文只从
  wayback `id_` / archive.today 快照取，不碰源站）。站点下线或全站 403 时仍能成批补历史内容，
  提取层与其他通道完全一致。
- 新增出口代理池 `crawler/proxies.py`：`via: proxy` + `proxy_pool:`，支持 http/https/socks5、
  每域粘性、失败冷却、日志脱敏；`collector/proxy_check.py` 打印每个出口的真实 IP。
- 接入 13ft（wasi-master/13ft）取文链路：`crawler/paywall.py` 在 403/挑战页/付费墙命中时按站点开关
  依次尝试 Googlebot 直取、Freedium（Medium）、archive.org 的 `id_` 快照、archive.today 的 `newest/`
  快照，或转发到自建 13ft 服务的 `POST /article`。仅正文确有增量才判为恢复，默认关闭，
  失败仍按原语义记 `http_403` / `access_restricted`。

动态网页渲染需要安装 Playwright 和 Chromium。PDF 解析依赖已写入锁文件。

验证通过 236 项测试，81 个信源的配置校验无错误。LNC 的四篇公开存档样本保留了全部 20 段核对正文。
