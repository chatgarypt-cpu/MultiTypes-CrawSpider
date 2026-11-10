#!/usr/bin/env bash
# 关闭 ego lite 浏览器引擎进程（不动系统 Chrome、不动 DSH web 服务）。
#
# 注意：DSH Web 的 dsh-ego-browser 插件会通过 ego-cast-worker（父进程是 dsh web server）
# 在数秒内重新拉起 ego lite。想彻底避免 ego 常驻，需要在 DSH web profile 里禁用该浏览器
# 插件，而不是反复 kill 进程；本脚本只用于临时释放 ego 占用的 Chrome 单实例。
set -u
collect_pids() {
  { pgrep -f "/Applications/ego lite.app/Contents/MacOS/ego lite" || true
    pgrep -f "/Applications/ego lite.app/Contents/Frameworks/ego Framework.framework/.*/Helpers/" || true
  } | sort -u
}
pids=$(collect_pids)
if [ -z "$pids" ]; then
  echo "ego: 没有运行中的 ego lite 进程"
  exit 0
fi
echo "ego: 停止 $(echo "$pids" | wc -l | tr -d ' ') 个进程（若 DSH web 插件开着，几秒后可能被重新拉起）"
kill -TERM $pids 2>/dev/null || true
sleep 3
left=$(collect_pids)
if [ -n "$left" ]; then
  kill -KILL $left 2>/dev/null || true
fi
echo "ego: 已发停止信号；系统 Chrome 和 DSH Web 服务不受影响"
