#!/usr/bin/env python3
"""Collect articles into the 32-column target schema. One site per run."""
import argparse, html, json, os, re, subprocess, sys, tempfile, time
import requests, yaml

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
COLS = ["title","sub_title","url","channel_text","country_text","lang_text","user_id","user_author_name","user_url",
        "created_at","description","text","image","video","link","view_count","like_count","reply_count",
        "retweet_count","dislike_count","favourite_count","tag","keyword","host","first_channel","second_channel",
        "third_channel","editor","updated_at","hit_num","read_num"]

def clean(h):
    if not h: return ""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|h\d|li|blockquote)>", "\n\n", h)
    h = re.sub(r"(?s)<[^>]+>", "", h)
    h = html.unescape(h)
    h = re.sub(r"[ \t\r]+", " ", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()

def iframes(h):
    return re.findall(r'<iframe[^>]+src=["\']([^"\']+)', h or "", re.I)

def first_img(h):
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', h or "", re.I)
    return m.group(1) if m else ""

def map_post(p, site):
    emb = p.get("_embedded", {})
    terms = emb.get("wp:term", []) or []
    cats = [t["name"] for group in terms for t in group if t.get("taxonomy") == "category"]
    tags = [t["name"] for group in terms for t in group if t.get("taxonomy") == "post_tag"]
    author = (emb.get("author") or [{}])[0]
    content = p.get("content", {}).get("rendered", "")
    yimg = ((p.get("yoast_head_json") or {}).get("og_image") or [{}])[0].get("url", "")
    frames = iframes(content)
    return {
        "title": clean(p.get("title", {}).get("rendered", "")),
        "sub_title": "",
        "url": p.get("link", ""),
        "channel_text": cats[0] if cats else "",
        "country_text": site.get("country", ""),
        "lang_text": site.get("lang", ""),
        "user_id": str(p.get("author") or ""),
        "user_author_name": author.get("name", ""),
        "user_url": author.get("link", ""),
        "created_at": (p.get("date_gmt") or "").replace(" ", "T") + ("Z" if p.get("date_gmt") else ""),
        "description": clean(p.get("excerpt", {}).get("rendered", "")),
        "text": clean(content),
        "image": p.get("jetpack_featured_media_url") or yimg or first_img(content),
        "video": next((f for f in frames if re.search(r"youtube|vimeo|dailymotion", f)), ""),
        "link": next((f for f in frames if not re.search(r"youtube|vimeo|dailymotion", f)), ""),
        "view_count": "", "like_count": "", "reply_count": "", "retweet_count": "", "dislike_count": "",
        "favourite_count": "", "tag": "; ".join(tags), "keyword": "", "host": site["domain"],
        "first_channel": "", "second_channel": "", "third_channel": "", "editor": "",
        "updated_at": (p.get("modified_gmt") or "").replace(" ", "T") + ("Z" if p.get("modified_gmt") else ""),
        "hit_num": "", "read_num": "",
    }

def get_batch(urls, site):
    """Return [(url, status, text)] — direct HTTP, or one Browserbase session for `via: browser`."""
    if site.get("via") == "browser":
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("\n".join(urls)); inp = f.name
        out = inp + ".ndjson"
        subprocess.run(["node", "research/bb_batch.mjs", site["base"] + "/", inp, out],
                       check=True, capture_output=True, text=True)
        res = [json.loads(l) for l in open(out) if l.strip()]
        os.unlink(inp); os.unlink(out)
        return [(r["url"], r["status"], r.get("text", "")) for r in res]
    out = []
    for u in urls:
        try:
            r = requests.get(u, headers={"User-Agent": UA}, timeout=90)
            out.append((u, r.status_code, r.text))
        except Exception as e:
            out.append((u, 0, str(e)))
        time.sleep(0.5)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    sites = yaml.safe_load(open("research/sources.yaml"))
    site = next(s for s in sites if s["domain"] == a.site)

    if site["channel"] != "wp-api":
        sys.exit("only wp-api implemented in this pilot")
    per = 50
    pages = []
    got = []
    page = 1
    while len(got) < a.limit:
        url = f"{site['base']}/wp-json/wp/v2/posts?per_page={per}&page={page}&_embed=1&orderby=date&order=desc"
        pages.append(url); page += 1
        if page > 20: break
        break  # ponytail: fetch one page per round, extend when cursor is verified
    for u, status, body in get_batch(pages, site):
        if status != 200:
            sys.exit(f"API {status} for {u}: {body[:200]}")
        for p in json.loads(body):
            got.append(map_post(p, site))
            if len(got) >= a.limit: break

    out = a.out or f"output/{site['domain']}/articles.jsonl"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for row in got:
            f.write(json.dumps({k: row[k] for k in COLS}, ensure_ascii=False) + "\n")
    print(f"{site['domain']}: {len(got)} rows -> {out}")
    lengths = [len(r["text"]) for r in got]
    print(f"text len min/avg/max: {min(lengths)}/{sum(lengths)//len(lengths)}/{max(lengths)}")
    print("sample:", json.dumps({k: (v[:90] if isinstance(v, str) else v) for k, v in got[0].items()}, ensure_ascii=False)[:600])

if __name__ == "__main__":
    main()
