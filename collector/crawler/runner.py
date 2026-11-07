"""Run one site: pick the channel module, log the run, refresh the per-site CSV."""
import json, logging, os, sys, time
from datetime import datetime, timezone

from crawler.config import DATA
from crawler.store import db, existing_urls, load_cursor
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
    except Exception:
        pass


def collect_site(site, limit, kind="posts"):
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
    r = runner(site, limit, log, fetcher, kind)

    c = db(); now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c.execute("INSERT INTO runs(domain,channel,started,finished,discovered,saved,errors,status,note) VALUES(?,?,?,?,?,?,?,?,?)",
              (site["domain"], channel, now, now, r.get("discovered") or r.get("pages") or 0,
               r.get("saved", 0), r.get("errors", 0), "ok" if r.get("saved") else "fail", ""))
    c.commit()

    if channel in ("wp-api", "json-api", "json-list", "sitemap", "rss", "html"):
        _export(site, kind)

    r.setdefault("secs", round(time.time() - t0, 1))
    return r
