#!/usr/bin/env python3
import json, csv, os, collections

src = list(csv.DictReader(open("research/sources.csv", encoding="utf-8")))
probe = {x["domain"]: x for x in json.load(open("research/probe_results.json"))}
dns = json.load(open("research/dns_check.json"))
ext = {x["domain"]: x for x in json.load(open("research/external_check.json"))}
brow = {}
if os.path.exists("research/browser_check.jsonl"):
    for line in open("research/browser_check.jsonl"):
        line = line.strip()
        if not line: continue
        try:
            r = json.loads(line); brow[r["domain"]] = r
        except Exception: pass

rows = []
for s in src:
    d = s["domain"]; p = probe.get(d, {})
    row = dict(s)
    # --- status ---
    status = "unknown"; why = ""
    ds = dns.get(d, {})
    pstatus = p.get("home_status")
    if ds.get("status") == 3 and d not in ext:
        status, why = "dns-dead", "NXDOMAIN"
    if d in ext:
        if ext[d].get("alive"):
            status = "alive"
        elif any("could not be resolved" in (e.get("msg") or "") for e in ext[d].get("errors", [])):
            status, why = "dns-dead", "NXDOMAIN (jina)"
        elif any("EAI_AGAIN" in (e.get("msg") or "") for e in ext[d].get("errors", [])):
            status, why = "dns-dead", "DNS fail (jina)"
        else:
            status, why = "unreachable", "external fetch failed"
    if pstatus == 200 and not p.get("cf"):
        status, why = "alive", ""
    if p.get("cf") and pstatus == 403:
        status, why = "cf-blocked", "Cloudflare challenge (local)"
    if pstatus in (0, 502):
        status = status if status == "alive" else "unreachable"
        why = why or f"local {pstatus}"
    row.update({"status": status, "why": why,
                "local_status": pstatus, "cf": bool(p.get("cf")),
                "generator": p.get("generator", "")[:40],
                "server": p.get("server", "")[:30]})
    # --- discovery channels ---
    wp = p.get("wp", {}) or {}
    b = brow.get(d, {})
    wp_ok = bool(wp.get("ok")) or (b.get("wpStatus") == 200 and b.get("ok"))
    row["wp_api"] = wp_ok
    row["wp_total"] = (wp.get("total") if wp.get("ok") else
                       (int(b["wpTotal"]) if b.get("wpTotal") and str(b.get("wpTotal")).isdigit() else None))
    row["feeds"] = len(p.get("feeds", []))
    row["feed_items"] = max([f.get("items", 0) for f in p.get("feeds", [])] or [0])
    row["feed_latest"] = (p.get("feeds") or [{}])[0].get("latest", "")
    sm = p.get("sitemaps", [])
    row["sitemap"] = sm[0]["path"] if sm else ""
    row["sitemap_urls"] = sm[0]["urls"] if sm else 0
    row["browser_ok"] = bool(b) and "err" not in b
    row["browser_articles"] = b.get("arts", "")
    row["browser_date_links"] = b.get("links", "")
    if wp_ok: row["channel"] = "wp-api"
    elif row["feeds"]: row["channel"] = "rss"
    elif sm: row["channel"] = "sitemap"
    elif row["browser_date_links"]: row["channel"] = "html-listing"
    else: row["channel"] = "none"
    rows.append(row)

cols = ["idx","name_cn","name_en","country","type","lang","domain","status","why","local_status","cf",
        "generator","wp_api","wp_total","feeds","feed_items","feed_latest","sitemap","sitemap_urls",
        "browser_ok","browser_articles","browser_date_links","channel"]
with open("research/summary.csv","w",newline="",encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)

print("== status =="); [print(f"  {k:<12} {v}") for k,v in collections.Counter(r["status"] for r in rows).most_common()]
print("== channel =="); [print(f"  {k:<12} {v}") for k,v in collections.Counter(r["channel"] for r in rows).most_common()]
print("== status x type ==")
for t in ("新闻","政府网站","智库"):
    c = collections.Counter(r["status"] for r in rows if r["type"] == t)
    print(f"  {t:<6}", dict(c))
print("== wp totals ==", sum(r["wp_total"] or 0 for r in rows), "across", sum(1 for r in rows if r["wp_total"]))
print("== dead list ==")
for r in rows:
    if r["status"] in ("dns-dead","unreachable"): print(f"  {r['domain']:<32} {r['status']:<10} {r['why']}")
