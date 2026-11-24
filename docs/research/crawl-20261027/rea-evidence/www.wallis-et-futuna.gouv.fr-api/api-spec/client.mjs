// Auto-generated API client from browser-trace capture.
// Usage: import {  } from './client.mjs';

const BASE = 'https://www.wallis-et-futuna.gouv.fr';

const defaultHeaders = {
  'Content-Type': 'application/json',
  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
};

async function request(path, { method = 'GET', body, query, headers } = {}) {
  let url = BASE + path;
  if (query) {
    const qs = new URLSearchParams(Object.entries(query).filter(([, v]) => v != null));
    if (qs.toString()) url += '?' + qs;
  }
  const res = await fetch(url, {
    method,
    headers: { ...defaultHeaders, ...headers },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

export async function getroot(options = {}) {
  return request('/', {
    method: 'GET',
    ...options,
  });
}

