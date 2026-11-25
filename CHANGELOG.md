# 更新日志

## Server 0.4.0 - 2026-11-25

- 试采支持站内公开 JSON 接口、JSON 列表和网页存档。样本验证通过后，队列沿用所选配置。订阅列表显示已启用的长期信源，加入记录提供重试入口。
- 试采可以发现同站公开接口、显示字段与分页线索，并生成通过校验的信源配置。已公开的 WordPress REST 和 Next.js 页面数据线索也会纳入候选。
- 修复已完成集合重复占用补采队列、部分短路响应使看板报网络错误，以及覆盖核对和采集写入之间的状态判断问题。
- 管理接口支持读取和保存采集设置。全局暂停期间保留网页队列，新任务等恢复后再领取。
- 服务器分发包使用空白信源模板。升级继续使用原有数据卷。

下载服务器源码包及 `SHA256SUMS`，校验并解压后执行 `sudo bash deploy/centos8/install.sh`。

## Server 0.3.0 - 2026-11-25

### 网页工作台

- 网页任务可加入长期采集。网页试采停止后，已采记录按 URL 合入主库，Supervisor 接续待采链接。
- 站点监控支持单站或批量刷新核对与补采。常见失败在主表显示简短原因，原文保留在任务详情。

### 安装与升级

- 服务器容器随网页服务启动 Supervisor，正式信源保存在数据卷内。升级时可迁移旧容器里的站点清单。

### 采集规则

- 新增全局放开开关 `data/CRAWL_BYPASS`（或 `CRAWL_BYPASS=1`）：存在即同时关闭 robots 闸门
  与 `disabled` 跳过，`sources.yaml` 不改也能整站全量采集；删除文件即恢复默认。环境变量显式写
  `0` 优先于哨兵文件，供测试隔离使用。
- `Fetcher` 在放开模式下列入 `robots=False`；`collect.py --all` 与 `supervisor.load_tasks()`
  在放开模式下同样收录 `disabled` 站点。`disabled` 依旧只作为分类记录，不再阻挡调度。
- 新增 `collector/tests/test_bypass.py` 覆盖开关的四种组合。

### 采集通道和断点

- 网页任务遇到接口缺失或采集不完整时，会沿公开栏目和分页查找详情。没有可见链接和正文的动态页面可用 Chromium 渲染。已识别的首页、栏目和分页只用于发现链接，正文和元数据提取均跳过这些入口页。
- 自动发现网页声明的同域 RSS / Atom 地址。订阅和栏目分页发现的同一篇文章只保存一次。
- 可按站点配置重试历史 HTTP 400，并用 Trafilatura 清理正文中的打印入口、账号提示和推荐区。可按配置执行一次历史补漏，中断后按断点续跑。
- JSON 列表的失败详情进入持久队列，落盘后才标记完成。分别记录列表付费标志和详情访问结果。配置中的重复键会在命令行启动时报告。
- 文档通道支持保存 PDF 原件并提取正文和元数据。重复原件按内容去重，解析失败保留在重试队列。
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
