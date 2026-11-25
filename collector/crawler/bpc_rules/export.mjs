// 把上游 sites.js（Bypass Paywalls Clean，MIT）转成运行时读的 rules.json。
// 规则本身一行不改：只做"JS 对象 → JSON"的序列化（RegExp 保留 source/flags）。
// 用法：node collector/crawler/bpc_rules/export.mjs
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

const src = fs.readFileSync(path.join(here, 'sites.js'), 'utf8') + '\nmodule.exports = defaultSites;\n';
const tmp = path.join(here, '.sites.tmp.cjs');
fs.writeFileSync(tmp, src);

let sites;
try {
  delete require.cache[require.resolve(tmp)];
  sites = require(tmp);
} finally {
  fs.rmSync(tmp, { force: true });
}

const out = {};
for (const [name, rule] of Object.entries(sites)) {
  const clean = {};
  for (const [key, value] of Object.entries(rule || {})) {
    if (value instanceof RegExp) clean[key] = { __regex: value.source, flags: value.flags };
    else if (typeof value === 'function') clean[key] = { __function: String(value) };
    else if (Array.isArray(value)) clean[key] = value.map(v => (v instanceof RegExp ? { __regex: v.source, flags: v.flags } : v));
    else clean[key] = value;
  }
  out[name] = clean;
}

const payload = {
  generated: new Date().toISOString().slice(0, 19),
  source: 'Bypass Paywalls Clean (sites.js) — MIT, magnolia1234',
  count: Object.keys(out).length,
  sites: out,
};
fs.writeFileSync(path.join(here, 'rules.json'), JSON.stringify(payload, null, 1) + '\n');
console.log(`rules.json: ${payload.count} 条规则`);
