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
REPO="/Users/gary/项目开发/REA_CrawlSpider"

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
printf 'www.lnc.nc:posts\n' >> "$REPO/data/CRAWL_SKIP"
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
cd /Users/gary/项目开发/REA_CrawlSpider
"演练场（ignore)/.venv/bin/python" collector/cli.py --help
"演练场（ignore)/.venv/bin/python" collector/cli.py status
"演练场（ignore)/.venv/bin/python" collector/cli.py paused
cat data/health.json
```
