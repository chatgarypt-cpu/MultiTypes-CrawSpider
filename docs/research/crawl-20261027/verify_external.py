#!/usr/bin/env python3
"""Verify ambiguous domains server-side via r.jina.ai (external fetch)."""
import json, re, time, urllib.request, urllib.error, concurrent.futures

d = json.load(open("research/probe_results.json"))
amb = [x for x in d if x.get("home_status") != 200 or x.get("cf") or x.get("home_status") in (403, 502, 0)]
print("ambiguous:", len(amb), flush=True)

def check(x):
    dm = x["domain"]
    out = {"domain": dm}
    for scheme in ("https", "http"):
        url = f"https://r.jina.ai/{scheme}://{dm}/"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "X-Return-Format": "text"})
            r = urllib.request.urlopen(req, timeout=70)
            body = r.read(300_000).decode("utf-8", "replace")
            code = r.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace"); code = e.code
        except Exception as e:
            body = str(e); code = 0
        if code == 200 and not body.lstrip().startswith("{"):
            out.update({"scheme": scheme, "code": 200, "alive": True,
                        "title": (re.search(r"Title:\s*(.+)", body).group(1).strip()[:90] if "Title:" in body else ""),
                        "len": len(body), "head": body[:400]})
            return out
        out.setdefault("errors", []).append({"scheme": scheme, "code": code, "msg": body[:160]})
        if code == 422 and "could not be resolved" in body:
            continue
    out["alive"] = False
    return out

res = []
with concurrent.futures.ThreadPoolExecutor(4) as ex:
    futs = [ex.submit(check, x) for x in amb]
    for i, f in enumerate(concurrent.futures.as_completed(futs), 1):
        res.append(f.result()); print(i, res[-1]["domain"], res[-1].get("alive"), flush=True)
json.dump(res, open("research/external_check.json", "w"), ensure_ascii=False, indent=1)
print("done")
