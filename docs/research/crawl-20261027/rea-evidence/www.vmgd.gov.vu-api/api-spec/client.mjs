// Auto-generated API client from browser-trace capture.
// Usage: import {  } from './client.mjs';

const BASE = 'https://www.vmgd.gov.vu';

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

export async function getmap_json(options = {}) {
  return request('/map.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_index_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/index.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_warnings_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/warnings.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_weather_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/weather.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_climate_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/climate.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_geohazards_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/geohazards.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_aviation_forecast_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/aviation-forecast.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_learn_more_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/learn-more.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_forecast_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/forecast.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_geohazards_volcanoes_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/geohazards/volcanoes.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_geohazards_volcanoes_east_epi_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/geohazards/volcanoes/east-epi.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_geohazards_volcanoes_yasur_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/geohazards/volcanoes/yasur.json', {
    method: 'GET',
    ...options,
  });
}

export async function get_next_data_93rGSos_KsGher1rYca2v_geohazards_volcanoes_lopevi_json(options = {}) {
  return request('/_next/data/93rGSos_KsGher1rYca2v/geohazards/volcanoes/lopevi.json', {
    method: 'GET',
    ...options,
  });
}

