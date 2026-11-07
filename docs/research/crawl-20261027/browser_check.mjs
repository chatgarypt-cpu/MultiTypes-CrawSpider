const fs = await import("node:fs/promises");
const domains = JSON.parse(await fs.readFile("/Users/gary/项目开发/REA_CrawlSpider/演练场（ignore)/research/blocked_list.json", "utf8"));
const task = await taskSpace("crawl-research-20261027");
const page = task.page("p1");
const outPath = "/Users/gary/项目开发/REA_CrawlSpider/演练场（ignore)/research/browser_check.jsonl";
for (const d of domains) {
  const rec = { domain: d };
  try {
    await page.goto("https://" + d + "/", { waitUntil: "domcontentloaded", timeout: 40000 });
    await page.waitForTimeout(2500);
    rec.title = (await page.title()).slice(0, 90);
    const info = await page.evaluate(async () => {
      const res = await fetch("/wp-json/wp/v2/posts?per_page=1", { headers: { accept: "application/json" } });
      const t = await res.text();
      const feed = document.querySelector('link[type="application/rss+xml"]')?.href || "";
      return { wpStatus: res.status, wpTotal: res.headers.get("x-wp-total"), ok: t.trim().startsWith("["), feed,
               arts: document.querySelectorAll("article").length,
               links: [...document.querySelectorAll("a")].filter(a => /\/20\d\d\//.test(a.href)).length,
               h1: (document.querySelector("h1")?.textContent || "").trim().slice(0, 80) };
    });
    Object.assign(rec, info);
  } catch (e) { rec.err = String(e.message).slice(0, 120); }
  await fs.appendFile(outPath, JSON.stringify(rec) + "\n");
  console.log(JSON.stringify(rec));
}
await task.finish({ keep: true });
