"""Browser egress through the local ego lite Chromium (no Browserbase needed).

The ego runtime runs a Node script; we generate one with the URL list embedded,
let it navigate each URL as a real document load, and read the HTML back.
Used by `via: ego` for Cloudflare / Akamai protected sites.
"""
import json, os, subprocess, tempfile

EGO_BIN = os.environ.get("EGO_BIN", "ego-browser")
CHUNK = int(os.environ.get("EGO_CHUNK", "12"))
SPACE = os.environ.get("EGO_SPACE", "collector-crawl")

SCRIPT = r"""
const fs = await import("node:fs/promises");
const urls = __URLS__;
const outPath = "__OUT__";
const space = "__SPACE__";
const jsonMode = __JSON__;
const tabs = __TABS__;
const out = new Array(urls.length);

let task;
try {
  task = await taskSpace(space);
} catch (e) {
  await fs.writeFile(outPath, JSON.stringify({error: String(e)}));
  process.exit(0);
}

async function tabFor(i) {
  const label = "p" + (i + 1);
  try { return task.page(label); } catch (_) {}
  try { return await task.newPage(); } catch (_) {}
  try { return task.page("p1"); } catch (_) {}
  return null;
}

async function grab(pg, idx) {
  const u = urls[idx];
  let status = 0, html = "";
  try {
    await pg.goto(u, { waitUntil: "domcontentloaded", timeout: 60000 });
    await pg.waitForTimeout(__WAIT__);
    const info = await pg.info();
    status = (info && (info.status || info.statusCode)) || 200;
    html = jsonMode
      ? await pg.evaluate(() => document.body ? document.body.innerText : "")
      : await pg.evaluate(() => document.documentElement.outerHTML);
  } catch (e) {
    status = 0;
    try {
      html = jsonMode ? await pg.evaluate(() => document.body ? document.body.innerText : "")
                      : await pg.evaluate(() => document.documentElement.outerHTML);
    } catch (_) {}
  }
  out[idx] = { url: u, status, html };
}

const pages = [];
for (let i = 0; i < Math.max(1, tabs); i++) {
  const pg = await tabFor(i);
  if (pg) pages.push(pg);
}
if (!pages.length) {
  await fs.writeFile(outPath, JSON.stringify({error: "no page"}));
  process.exit(0);
}

for (let i = 0; i < urls.length; i += pages.length) {
  const wave = pages.map((pg, j) => grab(pg, i + j)).filter((_, j) => i + j < urls.length);
  await Promise.all(wave);
}
await fs.writeFile(outPath, JSON.stringify(out.filter(Boolean)));
"""



def available():
    return subprocess.run(["which", EGO_BIN], capture_output=True).returncode == 0


def fetch_many(urls, timeout=600, json_mode=False, space=None, tabs=None, wait_ms=None):
    """Return [(url, status, body)] using the local ego browser.

    json_mode=True reads document.body.innerText (JSON endpoints render as a <pre>).
    wait_ms: 每页加载后等待 JS/挑战页完成的时间（默认 EGO_WAIT_MS=1000；Cloudflare
    挑战页通常要 2-3s）。
    """
    wait_ms = int(wait_ms or os.environ.get("EGO_WAIT_MS", "1000"))
    results = []
    for i in range(0, len(urls), CHUNK):
        part = urls[i:i + CHUNK]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            out_path = f.name
        script = (SCRIPT.replace("__URLS__", json.dumps(part))
                        .replace("__OUT__", out_path)
                        .replace("__SPACE__", space or SPACE)
                        .replace("__JSON__", "true" if json_mode else "false")
                        .replace("__WAIT__", str(wait_ms))
                        .replace("__TABS__", str(tabs or int(os.environ.get("EGO_TABS", "1")))))
        try:
            proc = subprocess.run([EGO_BIN, "nodejs"], input=script, capture_output=True,
                                  text=True, timeout=timeout)
            data = json.load(open(out_path))
        except Exception as e:
            data = [{"url": u, "status": 0, "html": ""} for u in part]
            if os.environ.get("EGO_DEBUG"):
                print("ego batch failed:", str(e)[:200])
        finally:
            try:
                os.unlink(out_path)
            except Exception:
                pass
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"ego runtime error: {data['error'][:200]}")
        for row in data:
            results.append((row["url"], row.get("status", 0), row.get("html", "")))
    return results
