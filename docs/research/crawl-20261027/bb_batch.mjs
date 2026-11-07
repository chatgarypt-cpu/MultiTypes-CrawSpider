// Fetch many same-origin URLs inside one Browserbase session (passes Cloudflare once).
// usage: node bb_batch.mjs <homeUrl> <urls.txt> <out.ndjson>
import fs from 'node:fs';
import { withSession } from './bb_fetch.mjs';

const [home, urlsFile, outFile] = process.argv.slice(2);
const urls = fs.readFileSync(urlsFile, 'utf8').split('\n').map(s => s.trim()).filter(Boolean);
const out = fs.createWriteStream(outFile);
let done = 0;
const t0 = Date.now();
await withSession(async ({ fetchPage, evaluate }) => {
  await fetchPage(home, { waitMs: 1200 });          // one real navigation to clear the challenge
  for (let i = 0; i < urls.length; i += 5) {
    const chunk = urls.slice(i, i + 5);
    const res = await evaluate(`(async () => {
      const urls = ${JSON.stringify(chunk)};
      const out = [];
      for (const u of urls) {
        try {
          const r = await fetch(u, { headers: { accept: '*/*' } });
          out.push({ url: u, status: r.status, text: await r.text() });
        } catch (e) { out.push({ url: u, status: 0, err: String(e).slice(0, 120) }); }
      }
      return out;
    })()`);
    for (const r of res || []) out.write(JSON.stringify(r) + '\n');
    done += chunk.length;
    console.error(`[bb] ${done}/${urls.length} ${Math.round((Date.now()-t0)/1000)}s`);
  }
});
out.end();
console.error(`[bb] done ${urls.length} in ${Math.round((Date.now()-t0)/1000)}s`);
