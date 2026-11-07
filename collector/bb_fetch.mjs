// Minimal Browserbase CDP client. One session can fetch many pages.
// CLI: node bb_fetch.mjs <url> [waitMs]
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

const env = Object.fromEntries(fs.readFileSync(new URL('../.env.local', import.meta.url), 'utf8')
  .split('\n').filter(Boolean).map(l => l.split('=')));
const KEY = process.env.BROWSERBASE_API_KEY || env.BROWSERBASE_API_KEY;
const PID = process.env.BROWSERBASE_PROJECT_ID || env.BROWSERBASE_PROJECT_ID;

const api = async (path, opt = {}) => {
  const r = await fetch('https://api.browserbase.com/v1' + path, {
    ...opt, headers: { 'X-BB-API-Key': KEY, 'Content-Type': 'application/json', ...(opt.headers || {}) },
  });
  return { status: r.status, body: await r.json().catch(() => null) };
};

export async function withSession(fn, { timeoutMs = 300000 } = {}) {
  const s = await api('/sessions', { method: 'POST', body: JSON.stringify({ projectId: PID, timeout: Math.ceil(timeoutMs / 1000) }) });
  if (s.status >= 300) throw new Error('session ' + s.status + ' ' + JSON.stringify(s.body));
  const session = s.body;
  const ws = new WebSocket(session.connectUrl);
  const pending = new Map();
  const listeners = [];
  let seq = 0;
  const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
    const id = ++seq; pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
    else if (m.method) listeners.forEach(f => f(m));
  };
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws error')); });
  try {
    const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    await send('Runtime.enable', {}, sessionId);
    const loadWait = () => new Promise((res) => {
      const t = setTimeout(res, 45000);
      listeners.push((m) => { if (m.method === 'Page.loadEventFired') { clearTimeout(t); res(); } });
    });
    const fetchPage = async (url, { waitMs = 800, evalJs = null } = {}) => {
      await send('Page.navigate', { url }, sessionId);
      // ponytail: poll readyState instead of waiting for `load`; heavy ad pages never finish loading
      const deadline = Date.now() + 30000;
      let ready = false;
      while (Date.now() < deadline) {
        const st = await send('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true }, sessionId);
        if (st.result?.value && st.result.value !== 'loading') { ready = true; break; }
        await new Promise(r => setTimeout(r, 200));
      }
      if (!ready) throw new Error('readyState timeout');
      // Cloudflare interstitials resolve themselves after a few seconds; title stays empty until then
      const cfDeadline = Date.now() + 25000;
      while (Date.now() < cfDeadline) {
        const t = await send('Runtime.evaluate', { expression: 'document.title', returnByValue: true }, sessionId);
        if (t.result?.value && !/just a moment|attention required|performing security/i.test(t.result.value)) break;
        await new Promise(r => setTimeout(r, 1500));
      }
      if (waitMs) await new Promise(r => setTimeout(r, waitMs));
      let html = await send('Runtime.evaluate', { expression: 'document.documentElement.outerHTML', returnByValue: true }, sessionId);
      if (!html.result?.value) {
        await new Promise(r => setTimeout(r, 1500));
        html = await send('Runtime.evaluate', { expression: 'document.documentElement.outerHTML', returnByValue: true }, sessionId);
      }
      const title = await send('Runtime.evaluate', { expression: 'document.title', returnByValue: true }, sessionId);
      let extra = null;
      if (evalJs) extra = (await send('Runtime.evaluate', { expression: evalJs, returnByValue: true, awaitPromise: true }, sessionId)).result?.value;
      return { url, title: title.result?.value, html: html.result?.value || '', extra };
    };
    const evaluate = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }, sessionId);
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'evaluate failed');
      return r.result?.value;
    };
    return await fn({ fetchPage, evaluate, sessionId: session.id });
  } finally {
    ws.close();
    await api('/sessions/' + session.id, { method: 'POST', body: JSON.stringify({ status: 'REQUEST_RELEASE' }) }).catch(() => {});
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [url, waitArg] = process.argv.slice(2);
  const r = await withSession(({ fetchPage }) => fetchPage(url, { waitMs: Number(waitArg || 2000) }));
  fs.writeFileSync('/tmp/bb_out.html', r.html);
  console.log(JSON.stringify({ title: r.title, bytes: r.html.length, cf: /Just a moment/i.test(r.html) }));
}
