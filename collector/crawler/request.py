"""Request model and a durable per-site request queue (sqlite).

Every URL a channel discovers becomes one Request row, so a run can stop at any
moment and resume, a single failed URL can be retried on its own, and batches are
streamed instead of being materialised in memory.

Table lives in data/state.db next to the existing runs table.
"""
import json, os, re, sqlite3, time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from crawler.config import DATA, DB

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                   "fbclid", "gclid", "ref", "referrer"}


def canonical_key(url: str) -> str:
    """Stable identity for a URL: lowercase host, no fragment, no tracking params, no trailing slash."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(sorted(query)), ""))


@dataclass
class Request:
    url: str
    channel: str = ""
    kind: str = "posts"
    label: str = "detail"
    state: str = "unprocessed"      # unprocessed | inflight | done | error | skipped
    retries: int = 0
    error: str = ""
    key: str = field(default="")

    def __post_init__(self):
        if not self.key:
            self.key = canonical_key(self.url)


class RequestQueue:
    """Durable queue scoped to one (domain, kind)."""

    def __init__(self, domain, kind="posts", db_path=None):
        self.domain = domain
        self.kind = kind
        self.path = db_path or DB
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        # WAL: collector, status/dashboard and reconcile can read while a run writes;
        # busy_timeout: wait out a concurrent writer instead of "database is locked";
        # NORMAL: durable enough for a resumable queue, much cheaper than FULL.
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS requests(
            domain TEXT, kind TEXT, key TEXT, url TEXT, channel TEXT, label TEXT,
            state TEXT, retries INTEGER DEFAULT 0, error TEXT,
            created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (domain, kind, key))""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS requests_state ON requests(domain, kind, state)")
        self.conn.commit()

    # ---------- writing ----------
    def add_many(self, urls, channel="", label="detail"):
        return self.add_labelled(((url, label) for url in dict.fromkeys(urls)), channel)

    def add_labelled(self, entries, channel):
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        rows = [(self.domain, self.kind, canonical_key(u), u, channel, label, "unprocessed", 0, "", now, now)
                for u, label in entries if u]
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO requests(domain,kind,key,url,channel,label,state,retries,error,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()
        return self.conn.total_changes - before

    def update_labels(self, entries):
        if not entries:
            return
        self.conn.executemany(
            "UPDATE requests SET label=? WHERE domain=? AND kind=? AND key=?",
            [(label, self.domain, self.kind, key) for key, label in entries])
        self.conn.commit()

    def seed_done(self, urls, channel=""):
        """A persisted row wins over stale queue state after an interrupted run."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        rows = [(self.domain, self.kind, canonical_key(u), u, channel, "detail", "done", 0, "", now, now)
                for u in urls if u]
        self.conn.executemany(
            "INSERT INTO requests(domain,kind,key,url,channel,label,state,retries,error,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(domain,kind,key) DO UPDATE SET "
            "state='done', error='', updated=excluded.updated WHERE requests.state<>'done'", rows)
        self.conn.commit()

    def requeue_missing_rows(self, known_urls):
        """Repair historical done-before-persist records against the disk URL registry."""
        known = {canonical_key(u) for u in known_urls}
        rows = self.conn.execute(
            "SELECT key FROM requests WHERE domain=? AND kind=? AND state='done'",
            (self.domain, self.kind)).fetchall()
        missing = [key for (key,) in rows if key not in known]
        self._set_state(missing, "unprocessed", error="missing_persisted_row")
        return len(missing)

    def mark_done(self, keys):
        self._set_state(keys, "done")

    def mark_skipped(self, keys, reason=""):
        self._set_state(keys, "skipped", error=reason)

    def mark_error(self, keys, errors, max_retries=3):
        """只更新这一批失败的 key。以前这里有一条整站 UPDATE，会把该域所有 done
        行重置成 unprocessed，导致每次出错都要把整站重抓一遍（matangi/lnc 都中过）。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        rows = []
        for key in keys:
            error = str(errors.get(key, ""))[:200]
            rows.append((error, now, 1 if self.permanent_error(error) else max_retries,
                         self.domain, self.kind, key))
        self.conn.executemany(
            "UPDATE requests SET retries = retries + 1, error = ?, updated = ?, "
            "state = CASE WHEN retries + 1 >= ? THEN 'error' ELSE 'unprocessed' END "
            "WHERE domain=? AND kind=? AND key=?", rows)
        self.conn.commit()

    RETRYABLE_MARKERS = ("http_0", "http_408", "http_425", "http_429", "http_5",
                         "document_error", "extraction_failed", "render_unverified")

    @staticmethod
    def permanent_error(error):
        return (error or "").startswith(("http_0 archive_miss", "http_0 PDF exceeds response size limit",
                                         "http_0 response_size_limit"))

    @classmethod
    def retryable_error(cls, error, statuses=()):
        error = error or ""
        if cls.permanent_error(error):
            return False
        match = re.match(r"http_(\d+)\b", error)
        return bool(match and int(match.group(1)) in statuses) or error.startswith(cls.RETRYABLE_MARKERS)

    def retry_transient(self, cooldown=900, statuses=()):
        """把上一轮耗尽的"临时故障"死信重新入队（跨运行恢复）。

        只在错误时间超过 cooldown 后重试，避免每轮立刻打同一个坏端点；404/410/403/
        robots 这类终态错误不动。每次重新入队会把 retries 清零，于是单次运行内仍
        按 mark_error 的 3 次上限重试，不会无限循环。
        """
        now = time.time()
        rows = self.conn.execute(
            "SELECT key, error, updated FROM requests WHERE domain=? AND kind=? AND state='error'",
            (self.domain, self.kind)).fetchall()
        retry = []
        for key, error, updated in rows:
            if not self.retryable_error(error, statuses):
                continue
            try:
                ts = time.mktime(time.strptime(updated, "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError, OverflowError):
                ts = 0
            if now - ts >= cooldown:
                retry.append(key)
        if retry:
            now_s = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.conn.executemany(
                "UPDATE requests SET state='unprocessed', retries=0, updated=? "
                "WHERE domain=? AND kind=? AND key=?",
                [(now_s, self.domain, self.kind, k) for k in retry])
            self.conn.commit()
        return len(retry)

    def retry_documents(self):
        """Only reopen legacy no_text records whose URL identifies a document."""
        from crawler.documents import is_document_url
        rows = self.conn.execute(
            "SELECT key,url FROM requests WHERE domain=? AND kind=? AND state='skipped' AND error='no_text'",
            (self.domain, self.kind)).fetchall()
        keys = [key for key, url in rows if is_document_url(url)]
        self._set_state(keys, "unprocessed")
        return len(keys)

    def requeue_failed(self, patterns=(), limit=0, dry_run=False):
        """把匹配错误前缀的终态行重新入队（补采用：付费墙回退、换出口后重抓）。

        只动 `error`/`skipped` 行，404/410 这类死链不传进 patterns 就不会被翻出来。
        返回 {"matched": n, "requeued": n, "by_error": {...}}。
        """
        patterns = [p for p in patterns if p]
        if not patterns:
            return {"matched": 0, "requeued": 0, "by_error": {}}
        where = " OR ".join("error LIKE ?" for _ in patterns)
        args = [self.domain, self.kind, *[p + "%" for p in patterns]]
        by_error = {}
        for err, n in self.conn.execute(
                f"SELECT error, COUNT(*) FROM requests WHERE domain=? AND kind=? "
                f"AND state IN ('error','skipped') AND ({where}) GROUP BY error", args).fetchall():
            label = next((pat for pat in patterns if (err or "").startswith(pat)), "other")
            by_error[label] = by_error.get(label, 0) + n
        matched = sum(by_error.values())
        if dry_run or not matched:
            return {"matched": matched, "requeued": 0, "by_error": by_error}
        sql = (f"SELECT key FROM requests WHERE domain=? AND kind=? "
               f"AND state IN ('error','skipped') AND ({where})")
        if limit:
            sql += " LIMIT %d" % int(limit)
        keys = [k for (k,) in self.conn.execute(sql, args).fetchall()]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.conn.executemany(
            "UPDATE requests SET state='unprocessed', retries=0, error='', updated=? "
            "WHERE domain=? AND kind=? AND key=?",
            [(now, self.domain, self.kind, k) for k in keys])
        self.conn.commit()
        return {"matched": matched, "requeued": len(keys), "by_error": by_error}

    def _set_state(self, keys, state, error=""):
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.conn.executemany(
            "UPDATE requests SET state=?, error=?, updated=? WHERE domain=? AND kind=? AND key=?",
            [(state, error, now, self.domain, self.kind, k) for k in keys])
        self.conn.commit()

    # ---------- reading ----------
    def next_batch(self, size=200):
        if size < 1:
            raise ValueError("QUEUE_BATCH must be a positive integer")
        cur = self.conn.execute(
            "SELECT url, channel, kind, label, state, retries, error, key FROM requests "
            "WHERE domain=? AND kind=? AND state='unprocessed' ORDER BY rowid LIMIT ?",
            (self.domain, self.kind, size))
        return [Request(*row) for row in cur.fetchall()]

    def stats(self) -> dict[str, int]:
        cur = self.conn.execute("SELECT state, COUNT(*) FROM requests WHERE domain=? AND kind=? GROUP BY state",
                                (self.domain, self.kind))
        return dict(cur.fetchall())

    def pending(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM requests WHERE domain=? AND kind=? AND state='unprocessed'",
            (self.domain, self.kind)).fetchone()[0]
