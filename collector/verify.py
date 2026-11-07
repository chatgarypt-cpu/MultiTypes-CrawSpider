#!/usr/bin/env python3
"""Post-run verification.

1. pagination: did the cursor walk past the last API page?
2. totals: API X-WP-Total vs posts actually seen vs rows saved
3. browser: open the newest and oldest saved article in a real browser and compare titles
"""
import argparse, csv, json, os, subprocess
import yaml, requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CSV_DIR = os.path.join(ROOT, "..", "output", "csv")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

def api_total(site):
    u = (f"{site['base']}/?rest_route=/wp/v2/posts&per_page=1" if site.get("api") == "rest_route"
         else f"{site['base']}/wp-json/wp/v2/posts?per_page=1")
    try:
        return int(requests.get(u, headers={"User-Agent": UA}, timeout=60).headers.get("X-WP-Total") or 0)
    except Exception:
        return 0

def load(domain):
    p = os.path.join(CSV_DIR, domain + ".csv")
    if not os.path.exists(p): return []
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def cursor(domain):
    p = os.path.join(DATA, domain, "cursor.json")
    return json.load(open(p)) if os.path.exists(p) else {}

BROWSER_TPL = """
const task = await taskSpace("verify-crawl-20261028");
const page = task.page("p1");
try {
  await page.goto(%s, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(2500);
  const d = await page.evaluate(() => ({ title: document.title, finalUrl: location.href }));
  console.log("RESULT " + JSON.stringify({ ok: true, ...d }));
} catch (e) {
  console.log("RESULT " + JSON.stringify({ ok: false, err: String(e.message).slice(0, 120) }));
}
"""

def open_in_browser(url):
    """ego lite can only run inside its own runtime, so feed it a one-shot script."""
    script = BROWSER_TPL % json.dumps(url)
    try:
        out = subprocess.run(["ego-browser", "nodejs"], input=script, capture_output=True, text=True, timeout=120)
        for line in reversed((out.stdout + "\n" + out.stderr).strip().splitlines()):
            if line.startswith("RESULT "):
                return json.loads(line[7:])
        return {"ok": False, "err": (out.stderr or out.stdout).strip()[-80:]}
    except Exception as e:
        return {"ok": False, "err": str(e)[:80]}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--site"); a = ap.parse_args()
    sites = [s for s in yaml.safe_load(open(os.path.join(ROOT, "sources.yaml"))) if s["channel"] == "wp-api"]
    if a.site: sites = [s for s in sites if s["domain"] == a.site]
    report = []
    for s in sites:
        d = s["domain"]
        rows = load(d)
        if not rows: continue
        cur = cursor(d)
        total = api_total(s)
        pages = cur.get("total_pages")
        seen = cur.get("seen")
        rec = {"domain": d, "api_total": total, "csv_rows": len(rows),
               "pages_done": cur.get("next_page"), "pages_total": pages,
               "seen": seen, "done": cur.get("done", False)}
        rec["pagination_complete"] = bool(pages and cur.get("next_page", 0) > pages) or bool(cur.get("done"))
        if seen is not None and total:
            rec["seen_vs_total"] = "ok" if seen >= total else f"short {total - seen}"
        # newest + oldest article in a real browser
        for label, row in (("newest", rows[0]), ("oldest", rows[-1])):
            b = open_in_browser(row["url"])
            if b.get("ok"):
                rec[f"browser_{label}"] = "ok" if (b.get("title") or "").strip()[:20] in (row["title"] or "")[:200] else f"title differs: {b.get('title','')[:40]}"
            else:
                rec[f"browser_{label}"] = f"open failed: {b.get('err','')[:40]}"
        report.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    json.dump(report, open(os.path.join(DATA, "verify_report.json"), "w"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
