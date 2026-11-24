#!/usr/bin/env python3
"""代理池体检：列出每个出口的真实 IP / 延迟 / 是否可用（只读，不改配置）。

  python collector/proxy_check.py                     # 检查 data/proxies.txt
  python collector/proxy_check.py --file my.txt -v
  python collector/proxy_check.py --direct            # 先看本机直连出口 IP

代理清单格式（一行一个，`#` 开头为注释）：

  http://127.0.0.1:8080
  http://user:pass@proxy.example.com:3128
  socks5://127.0.0.1:1080
  proxy.example.com:8080          # 省略 scheme 时按 http:// 处理

采集里怎么用：站点写 `via: proxy` + `proxy_pool: data/proxies.txt`（或单项 `proxy:`）。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crawler import proxies  # noqa: E402


def direct_ip(timeout=8):
    """本机直连出口 IP：换代理前后对比用。"""
    try:
        import requests
        r = requests.get("https://api.ipify.org", timeout=timeout)
        return r.text.strip() if r.ok else f"http_{r.status_code}"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"[:80]


def main(argv=None):
    ap = argparse.ArgumentParser(description="代理池体检（只读）")
    ap.add_argument("--file", default=proxies.POOL_FILE, help="代理清单路径")
    ap.add_argument("--timeout", type=float, default=8)
    ap.add_argument("--direct", action="store_true", help="同时打印本机直连出口 IP")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每个失败原因")
    args = ap.parse_args(argv)

    out = {"direct": None, "pool_file": args.file, "total": 0, "alive": 0, "results": []}
    if args.direct:
        out["direct"] = direct_ip(args.timeout)

    entries = proxies.load(args.file)
    out["total"] = len(entries)
    if not entries:
        print(json.dumps(out, ensure_ascii=False, indent=1) if args.json
              else f"没有代理清单：{args.file}（写一行一个代理即可；采集侧默认直连）")
        return 0

    t0 = time.time()
    out["results"] = proxies.healthcheck(entries, timeout=args.timeout)
    out["alive"] = sum(1 for r in out["results"] if r["ok"])
    out["elapsed"] = round(time.time() - t0, 1)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if args.direct:
        print(f"直连出口 IP: {out['direct']}")
    print(f"代理池 {out['total']} 个，可用 {out['alive']} 个（{out['elapsed']}s）")
    for r in out["results"]:
        if r["ok"]:
            print(f"  ✓ {r['proxy']:38s} ip={r['ip']}")
        else:
            print(f"  ✗ {r['proxy']:38s} {r['error'] if args.verbose else ''}".rstrip())
    return 0 if out["alive"] else 1


if __name__ == "__main__":
    sys.exit(main())
