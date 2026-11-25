"""Run one site: pick the channel module, log the run, refresh the per-site CSV."""
import json, logging, os, sys, time
from contextlib import closing
from datetime import datetime, timezone

from crawler.config import DATA
from crawler.store import collection_lock, db, existing_urls, load_cursor, _paths
from crawler.web import Fetcher
from crawler.channels import REGISTRY
from crawler.schema import validate_site

CHANNELS = REGISTRY


def site_logger(site):
    d = os.path.join(DATA, site["domain"]); os.makedirs(d, exist_ok=True)
    lg = logging.getLogger("collect." + site["domain"])
    if not lg.handlers:
        lg.setLevel(logging.INFO)
        fh = logging.FileHandler(os.path.join(d, "run.log"), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        lg.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(sh)
    return lg


def _export(site, kind):
    try:
        import export_csv
        suffix = "" if kind == "posts" else "." + kind
        out = os.path.join(DATA, "csv", site["domain"] + suffix + ".csv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        export_csv.export(os.path.join(DATA, site["domain"]), out, kind=kind)
    except Exception as exc:
        logging.getLogger("collect." + site["domain"]).exception("CSV export failed")
        return str(exc)


def _network_error(err: str) -> bool:
    e = (err or "").lower()
    return (e.startswith("conn:") or e.startswith("http_0") or "max retries" in e
            or "connection" in e or "timed out" in e or "temporary failure" in e)


def _record_run(site, channel, kind, started, result):
    quality = result.get("quality")
    note = {"kind": kind, "error": result.get("error"), "skipped": result.get("skipped"),
            "reasons": result.get("reasons", {}), "quality": quality}
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO runs(domain,channel,started,finished,discovered,saved,errors,status,note) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (site["domain"], channel, started, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      quality["discovered"] if quality else result.get("discovered", 0),
                      result.get("saved", 0), result.get("errors", 0),
                      result.get("status") or ("interrupted" if result.get("error") else "ok"),
                      json.dumps(note, ensure_ascii=False)))


def collect_site(site, limit, kind="posts", *, export_csv=True, reset=False):
    with collection_lock(site["domain"], kind, wait=False):
        if reset:
            try:
                os.unlink(_paths(site["domain"], kind)[2])
            except FileNotFoundError:
                pass
        return _collect_site(site, limit, kind, export_csv=export_csv)


def _collect_site(site, limit, kind, *, export_csv):
    t0 = time.time()
    site = dict(site, via=site.get(f"via_{kind}") or site.get("via"))
    log = site_logger(site)
    channel = site.get("channel")
    runner = CHANNELS.get(channel)
    if runner is None:
        raise ValueError(f"unknown channel {channel!r} for {site['domain']}")

    errors, warnings = validate_site(dict(site, channel=channel))
    for w in warnings:
        log.info("config warning: %s", w)
    if errors:
        raise ValueError("; ".join(errors))

    fetcher = Fetcher(site)
    net_retry = int(os.environ.get("NET_RETRY", "2"))
    net_backoff = float(os.environ.get("NET_BACKOFF", "300"))
    attempt = 0
    while True:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            r = runner(site, limit, log, fetcher, kind)
        except Exception as exc:
            _record_run(site, channel, kind, started, {"status": "failed", "error": type(exc).__name__})
            raise
        _record_run(site, channel, kind, started, r)
        err = str(r.get("error") or "")
        if not _network_error(err) or attempt >= net_retry:
            break
        delay = net_backoff * (2 ** attempt)
        attempt += 1
        r["network_retries"] = attempt
        log.warning("%s network error (%s), retry %d/%d in %.0fs",
                    site["domain"], err[:80], attempt, net_retry, delay)
        time.sleep(delay)

    if export_csv and channel in ("wp-api", "json-api", "json-list", "sitemap", "rss", "html"):
        export_error = _export(site, kind)
        if export_error:
            r["export_error"] = export_error

    r.setdefault("secs", round(time.time() - t0, 1))
    return r
