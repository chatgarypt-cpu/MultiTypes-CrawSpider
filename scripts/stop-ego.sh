#!/usr/bin/env bash
# 关闭 ego lite（agent 浏览器）及 DSH 的 ego-cast worker。
# ego 是常驻 app：它会复用系统 Google Chrome 的单实例，运行期间本地 Chrome 可能打不开；
# 用完（尤其抓取结束后）应执行本脚本，避免长期占用。
set -u
collect_pids() {
  { pgrep -f "ego[ ]lite" || true
    pgrep -f "ego[ ]Framework" || true
    pgrep -f "ego-cast-worker" || true
    pgrep -f "ego-browser" || true
  } | sort -u
}
pids=$(collect_pids)
if [ -z "$pids" ]; then
  echo "ego: 没有运行中的进程"
  exit 0
fi
echo "ego: 停止 $(echo "$pids" | wc -l | tr -d ' ') 个进程"
kill -TERM $pids 2>/dev/null || true
sleep 3
left=$(collect_pids)
if [ -n "$left" ]; then
  kill -KILL $left 2>/dev/null || true
fi
echo "ego: 已关闭（系统 Chrome 不受影响）"
