# 网页采集服务

普通使用者只需要浏览器。管理员先部署一次，再把网页地址和账号交给使用者。

## 使用者：试采或批量入队

1. 打开管理员提供的网页；若出现登录框，用户名为 `admin`，密码由管理员提供。
2. 在“试采预览”中填写站点或文章网址，核对采集通道、样本正文和诊断。HTML 类通道可修改字段 XPath 并复测，确认后加入队列。试采不写文章或断点，15 分钟内可确认。
3. 已知站点可切换“批量入队”，粘贴网址，每行一个。
4. 点击“加入 N 个网站”。重复域名自动合并，无效地址逐条显示原因，其余站点正常入队。
5. 等待任务执行。可以关闭页面，之后重新打开查看；需要暂停时点“停止”，中断后点“继续采集”。
6. 任务结束后点击对应的“下载”获取 CSV。文章、页面、媒体元数据和其他公开集合分别导出。

「采集范围」支持通道默认范围、全部可发现内容、当前栏目和指定 URL 路径。当前栏目按提交的列表入口和详情链接规则发现内容。URL 路径按完整路径段匹配，例如 `/news` 包含 `/news/item`。WordPress REST 可单选或多选文章、页面、媒体元数据及站点公开的自定义类型。留空使用全部公开类型。修改范围后需重新试采才能确认入队。

试采支持 HTTP 直连的 HTML、RSS、站点地图和 WordPress REST。其他通道可继续使用批量入口。试采限额仅用于预览。

网页任务单独保存在 `数据目录/web-jobs/任务编号/`，不改动原有长任务的数据。顶部“并发”控制同时运行的网站任务数；适配器内请求并发沿用原配置（通用批量抓取使用 `SITE_WORKERS`）；无适配器的 HTML 链接遍历逐页处理。队列不会因为批量数或并发数截断网站总量。原有脚本数据展示在页面下半部分。

“本轮完成”表示当前适配器或已发现链接处理完毕，不等于独立证明网站全部历史内容齐全。WordPress 自动发现公开内容类型；站点地图遍历子地图；没有适配器的普通 HTML 站点递归遍历同域链接。RSS 不作为历史全量证明。登录内容、附件二进制文件、仅由脚本生成的链接、日历等无限 URL 空间仍需要专门适配；系统会保留失败状态和日志。已有站点适配器的范围会在任务中说明。

## 覆盖率更新

核对结果按站点保存到 `数据目录/coverage-sites/`，历史报告保留。页面显示核对时间、差额和失败原因；数据或断点状态变化后，旧结论会标为“待刷新”，不再作为最新通过结果。

网页服务每分钟检查已登记站点。数据变化或超过 `COVERAGE_TTL_HOURS`（默认 24 小时）后自动重新核对。首次安装时登记列表为空。核对失败时也会保存时间和原因。

要登记或立即重新核对其他站点，可运行：

```bash
.venv/bin/python collector/verify_coverage.py --site example.com
```

“已核对”沿用 95% 阈值，不等于零缺口；页面会另外列出差额。无法取得完整独立清单的站点显示“无法核对”。

## 管理员：本机启动

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Node.js 22.12+。在项目根目录执行：

```bash
./deploy/start-web
```

首次自动建立 Python 环境、按锁文件安装依赖、构建前端并启动。打开 <http://127.0.0.1:8787/>。脚本可从任何工作目录调用。端口被占用会直接报错，不会悄悄换端口。

已构建的部署环境可跳过构建：

```bash
CRAWL_SKIP_BUILD=1 ./deploy/start-web --host 127.0.0.1 --port 8787
```

环境变量：

| 变量 | 用途 | 默认值 |
|---|---|---|
| `CRAWL_HOST` / `CRAWL_PORT` | 监听地址 / 端口 | `127.0.0.1` / `8787` |
| `CRAWL_DATA_DIR` | 持久化数据目录，建议用绝对路径 | 项目内 `collector/data` |
| `CRAWL_WEB_PASSWORD` | 网页管理员密码，用户名为 `admin` | 本机未设置时免登录；对外监听必须设置 |
| `SITE_WORKERS` | 普通页面每站请求并发 | 4 |
| `CRAWL_PREVIEW_SAMPLES` | 试采最多显示的正文样本数 | 3 |
| `CRAWL_PREVIEW_DISCOVERY_REQUESTS` | 单次试采用于列表、RSS 或站点地图的发现请求数 | 8 |
| `CRAWL_SKIP_BUILD` | 已构建环境跳过 npm 安装和构建 | 0 |

不要把密码写入源码。网页服务面向可信管理员和操作人员，不是开放的匿名抓取代理；未设置密码时仅监听本机。私网目标默认拒绝，测试专用的 `CRAWL_ALLOW_PRIVATE=1` 不用于公网部署。公网访问应在 HTTPS 反向代理后提供服务，SSE 路径 `/api/stream` 关闭代理缓冲。

## 管理员：另一台 Linux 服务器

有 Docker Compose 的服务器，在项目根目录执行：

```bash
read -rs -p '设置网页密码: ' CRAWL_WEB_PASSWORD; echo
export CRAWL_WEB_PASSWORD
docker compose -f deploy/compose.yaml up -d --build
```

默认只映射到服务器本机 `127.0.0.1:8787`，由已有 HTTPS 反向代理转发到该端口。需要在可信局域网直接访问时，在启动前设置 `CRAWL_BIND=0.0.0.0`；远程明文 HTTP 不用于传送管理员密码。

镜像构建前端，运行时无需 Node。数据使用 `crawler-data` 卷，重建镜像不会删除数据；不要执行 `down -v`，该选项会删除数据卷。容器重启时中断的任务显示“已中断”，从网页继续；排队任务自动调度。单独重启网页进程时仍在运行的 worker 可通过文件锁识别。

```bash
docker compose -f deploy/compose.yaml logs -f
docker compose -f deploy/compose.yaml down
```

此基础镜像支持 HTTP / TLS 指纹请求；未打包本机 Ego 或浏览器运行时。需要 Playwright / Camoufox 的站点应在服务器另外安装项目 browser extra 及浏览器运行依赖并验证，不能把本机浏览器会话当作可迁移部署能力。

健康检查：`GET /healthz`。同一个数据目录只允许一个网页服务调度器。迁移时停止服务和采集任务后备份完整数据目录或 Docker 数据卷；不要仅复制 CSV，断点信息在任务目录和 SQLite 中。

## 历史脚本运维参考

以下是原有脚本和 macOS 调度方式，网页服务不依赖这些个人电脑配置。

# 部署说明：launchd 守护 + cron 定时任务

本目录包含：

| 文件 | 用途 |
|---|---|
| `com.reacrawl.supervisor.plist` | launchd 服务：常驻 + KeepAlive 跑 `collector/supervisor.py` |
| `collectorctl` | CLI 包装脚本（用项目 venv 跑 `collector/cli.py`） |
| `crontab.example` | cron 示例：每日增量 / 每周 verify+deliver / 每月 dedupe |

> 路径注意：仓库与 venv 路径含中文和空格，脚本里都带引号；仓库搬家后要同步改
> plist 里的三处路径和 `deploy/collectorctl` 里的 `PY`。
>
> 依赖（2026-11 起）：仓库根 `pyproject.toml` + `uv sync` 生成 `.venv`；`collectorctl`
> 优先用它、找不到再回退 `演练场（ignore)/.venv`。下面的 launchd/cron 仍在用旧
> `演练场（ignore)/.venv`（依赖一致，可继续跑）；要统一到 `.venv` 需改 plist/crontab
> 路径并 `launchctl bootout + bootstrap` 重载。

---

## 1. launchd（推荐，KeepAlive 常驻）

### 安装

```bash
REPO="$(pwd -P)"  # 在项目根目录执行

# 1) 先手动跑一次，确认能启动（Ctrl-C 退出）
cd "$REPO"
"演练场（ignore)/.venv/bin/python" collector/supervisor.py

# 2) 拷进用户级 LaunchAgents 并加载
mkdir -p ~/Library/LaunchAgents
cp "$REPO/deploy/com.reacrawl.supervisor.plist" ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.reacrawl.supervisor.plist
launchctl kickstart -k gui/$(id -u)/com.reacrawl.supervisor

# 3) 确认
launchctl print gui/$(id -u)/com.reacrawl.supervisor | head -30
tail -f "$REPO/data/supervise.log"
```

### 卸载 / 重启

```bash
launchctl bootout gui/$(id -u)/com.reacrawl.supervisor
rm -f ~/Library/LaunchAgents/com.reacrawl.supervisor.plist

# 改完代码重启（KeepAlive 会自动拉起，无需 kickstart；要强制重启用）
launchctl kickstart -k gui/$(id -u)/com.reacrawl.supervisor
```

### 日志与状态文件

| 路径 | 内容 |
|---|---|
| `data/supervise.log` | 守护事件日志（程序自己写：启停/重试/看门狗/暂停） |
| `data/supervise.out` | launchd 捕获的 stdout（内容与 supervise.log 近似） |
| `data/supervise.err` | launchd 捕获的 stderr（未捕获异常栈） |
| `data/supervise_state.json` | 每 30s 落盘：running / pending / completed / stalled / attempts |
| `data/health.json` | 每 30s 落盘：pid / running / pending / stalled / done / paused / total_rows / rate_5m / errors_1h / last_error |
| `data/crawl-<domain>-<kind>.log` | 每个采集子进程的日志 |
| `data/supervise.log` 追加写入，不自动轮转；建议每月 `mv` 一次或配 newsyslog |

### 暂停 / 恢复 / 跳过

```bash
"$REPO/deploy/collectorctl" pause     # 新建 data/CRAWL_PAUSED，守护 5s 内停掉子进程
"$REPO/deploy/collectorctl" resume    # 删除开关，自动恢复
"$REPO/deploy/collectorctl" paused    # 查看开关 + 守护状态

# 按站暂停：data/CRAWL_SKIP 每行一个 domain:kind
printf 'example.org:posts\n' >> "$REPO/data/CRAWL_SKIP"
```

### 可选：看板也交给 launchd

看板不是常驻必需；需要时用 cron/`nohup` 启动即可：

```bash
cd "$REPO" && nohup "演练场（ignore)/.venv/bin/python" collector/serve.py \
  >> data/serve.log 2>&1 &
# http://127.0.0.1:8787/ui/
```

---

## 2. cron（定时任务，与 launchd 可共存）

见 [`crontab.example`](crontab.example)。安装：

```bash
crontab -e          # 把 crontab.example 内容粘进去（按需去掉注释）
crontab -l          # 查看
```

要点：

- cron 的 `PATH` 很小，示例里全部用绝对路径的 venv python；
- 含空格/中文的路径一律加引号；
- 日志统一写 `data/cron-*.log`，便于和守护日志区分；
- `collectorctl verify` 会联网访问各站 API/sitemap，失败不影响已有快照；
- `collectorctl deliverables` 默认生成当天目录；补历史日期用 `--date YYYYMMDD`。

---

## 3. 快速自检清单

```bash
cd "$REPO"
"演练场（ignore)/.venv/bin/python" collector/cli.py --help
"演练场（ignore)/.venv/bin/python" collector/cli.py status
"演练场（ignore)/.venv/bin/python" collector/cli.py paused
cat data/health.json
```

---

## Linux 服务器（脱离 ego 依赖）

`ego` 是本机 macOS 浏览器（`ego-browser` CLI + 持久 profile），Linux 服务器上没有。需要浏览器的
站点改用 Playwright 通道（`via: playwright`，2026-11-09 实测可过 Cloudflare JS 挑战，与 ego 产出
正文一致）：

```bash
cd "$REPO"
uv sync --extra browser                          # 安装 playwright（可选依赖，不进默认安装）
python -m playwright install --with-deps chromium  # 装 Chromium + 系统依赖（Linux 需要）
# 站点配置示例
#   via: playwright
#   playwright_wait_ms: 3000     # CF 挑战页通常 2-3s
#   block_resources: false       # 仅文本抽取时可试 true；实测无稳定提速
```

- 本机验证可复用已有 Chromium：`PLAYWRIGHT_EXECUTABLE=/path/to/chrome ...`。
- `via: ego` 的站点在服务器上要逐个改成 `via: playwright`；ego 只保留作本地侦察/对照。
- Cloudflare Turnstile 级风控未验证；如遇到再评估 Patchright / playwright-stealth 或远程浏览器服务。
- 守护进程用 systemd（或 supervisord）替代 launchd：入口仍是
  `"$REPO/.venv/bin/python" collector/supervisor.py`，环境变量 `CRAWL_RATE/CRAWL_MAX_RATE/SITE_WORKERS` 同本机。
