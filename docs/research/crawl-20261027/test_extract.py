#!/usr/bin/env python3
"""Sample 1-2 articles per reachable site; measure metadata+text extraction with trafilatura."""
import json, re, time
import concurrent.futures as cf
import requests, trafilatura
from urllib.parse import urljoin

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
     "Accept-Language": "en-US,en;q=0.9,fr;q=0.8"}
probe = json.load(open("research/probe_results.json"))

def article_urls(x):
    urls = []
    if x.get("wp", {}).get("ok"):
        api = x["wp"]["api"]
        try:
            r = requests.get(api + "?per_page=2&_fields=link,date,title", headers=H, timeout=25)
            for p in r.json():
                urls.append(p["link"])
        except Exception:
            pass
    for f in x.get("feeds", []):
        if f.get("link_sample", "").startswith("http"):
            urls.append(f["link_sample"])
    for s in x.get("sitemaps", []):
        try:
            r = requests.get(x["base"] + s["path"], headers=H, timeout=25)
            locs = [l for l in re.findall(r"<loc>([^<]+)</loc>", r.text)
                    if re.search(r"/20\d\d/|/\d{4}/|article|story|news", l)]
            if s["type"] == "index":
                # follow first child sitemap
                for child in re.findall(r"<loc>([^<]+)</loc>", r.text)[:2]:
                    cr = requests.get(child, headers=H, timeout=25)
                    locs += re.findall(r"<loc>([^<]+)</loc>", cr.text)[:4]
            urls += locs[:2]
        except Exception:
            pass
    seen, out = set(), []
    for u in urls:
        if u not in seen and not re.search(r"\.(jpg|png|pdf|css|js)$", u):
            seen.add(u); out.append(u)
    return out[:2]

def one(x):
    res = {"domain": x["domain"], "country": x.get("country"), "type": x.get("type"),
           "status": x.get("home_status"), "cf": x.get("cf"), "generator": x.get("generator",""),
           "wp_total": x.get("wp", {}).get("total"), "feeds": len(x.get("feeds", [])),
           "sitemaps": [(s["path"], s["urls"]) for s in x.get("sitemaps", [])], "samples": []}
    for u in article_urls(x):
        s = {"url": u}
        try:
            r = requests.get(u, headers=H, timeout=30)
            s["status"] = r.status_code
            txt = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
            m = trafilatura.extract_metadata(r.text)
            s.update({"len": len(txt), "title": (m.title or "")[:70] if m else "",
                      "date": (m.date or "") if m else "", "author": (m.author or "")[:30] if m else "",
                      "sitename": (m.sitename or "")[:30] if m else "",
                      "lang": (m.language or "") if m else "",
                      "head": txt[:120].replace("\n", " ")})
        except Exception as e:
            s["err"] = type(e).__name__ + ":" + str(e)[:60]
        res["samples"].append(s)
    return res

good = [x for x in probe if x.get("home_status") == 200 and not x.get("cf")]
print("testing", len(good), flush=True)
out = []
with cf.ThreadPoolExecutor(6) as ex:
    for i, f in enumerate(cf.as_completed([ex.submit(one, x) for x in good]), 1):
        out.append(f.result()); print(i, out[-1]["domain"], flush=True)
json.dump(out, open("research/extract_test.json", "w"), ensure_ascii=False, indent=1)
print("done", len(out))
