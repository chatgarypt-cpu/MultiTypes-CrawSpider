"""reconcile() 测试：把 state='unprocessed' 但 url 已在 urls.txt 里的行改成 done。

事故来源：mark_error 曾整站 UPDATE，把 done 重置为 unprocessed；进程被杀/导入绕过
队列时也会出现"盘上已有、队列还说没做"的错位，reconcile 负责收敛。
"""
from crawler.request import RequestQueue, canonical_key
from crawler.reconcile import reconcile


def _seed(domain, urls, kind="posts", done=()):
    q = RequestQueue(domain, kind)
    q.add_many(urls)
    if done:
        q.mark_done([canonical_key(u) for u in done])
    q.conn.close()


def _stats(domain, kind="posts"):
    q = RequestQueue(domain, kind)
    out = q.stats()
    q.conn.close()
    return out


def _write_urls(tmp_env, domain, urls, kind="posts"):
    d = tmp_env / domain
    d.mkdir(parents=True, exist_ok=True)
    name = "urls.txt" if kind == "posts" else f"{kind}.urls.txt"
    with open(d / name, "w", encoding="utf-8") as fh:
        for url in urls:
            fh.write(url + "\n")


def test_reconcile_marks_stored_rows_done(tmp_env):
    domain = "rec.test"
    urls = [f"https://rec.test/p/{i}" for i in range(3)]
    _seed(domain, urls, done=[urls[0]])
    # 盘上已有 /0 和 /1（带 tracking 参数的变体也必须按 canonical key 命中）
    _write_urls(tmp_env, domain, [urls[0], urls[1] + "?utm_source=x"])

    out = reconcile(domain)

    assert out == {"updated": 1, "by_domain": {domain: 1}}
    assert _stats(domain) == {"done": 2, "unprocessed": 1}


def test_reconcile_dry_run_counts_but_does_not_write(tmp_env):
    domain = "dryrec.test"
    urls = [f"https://dryrec.test/p/{i}" for i in range(2)]
    _seed(domain, urls)
    _write_urls(tmp_env, domain, urls)

    out = reconcile(dry_run=True)

    assert out["updated"] == 2
    assert _stats(domain) == {"unprocessed": 2}
    # 真正的运行才落库
    assert reconcile()["updated"] == 2
    assert _stats(domain) == {"done": 2}


def test_reconcile_scoped_by_domain(tmp_env):
    for dom in ("a.test", "b.test"):
        _seed(dom, [f"https://{dom}/p"])
        _write_urls(tmp_env, dom, [f"https://{dom}/p"])

    out = reconcile(domain="a.test")

    assert out == {"updated": 1, "by_domain": {"a.test": 1}}
    assert _stats("a.test") == {"done": 1}
    assert _stats("b.test") == {"unprocessed": 1}
    # 不带 domain 时补上 b.test
    assert reconcile() == {"updated": 1, "by_domain": {"b.test": 1}}


def test_reconcile_reads_kind_specific_urls_file(tmp_env):
    domain = "media.test"
    _seed(domain, ["https://media.test/a.jpg"], kind="media")
    _write_urls(tmp_env, domain, ["https://media.test/a.jpg"], kind="media")

    assert reconcile()["updated"] == 1
    assert _stats(domain, "media") == {"done": 1}


def test_reconcile_without_db_is_a_noop(tmp_env):
    assert reconcile() == {"updated": 0, "by_domain": {}}
    assert not (tmp_env / "state.db").exists()      # dry 查询不能顺手建库


def test_reconcile_is_idempotent(tmp_env):
    domain = "idem.test"
    _seed(domain, ["https://idem.test/p"])
    _write_urls(tmp_env, domain, ["https://idem.test/p"])

    assert reconcile(domain)["updated"] == 1
    assert reconcile(domain) == {"updated": 0, "by_domain": {}}


def test_reconcile_leaves_non_unprocessed_rows_alone(tmp_env):
    domain = "states.test"
    urls = [f"https://states.test/p/{i}" for i in range(3)]
    q = RequestQueue(domain)
    q.add_many(urls)
    q.mark_skipped([canonical_key(urls[0])], "no_text")
    q.mark_error([canonical_key(urls[1])], {canonical_key(urls[1]): "http_500"})
    q.conn.close()
    _write_urls(tmp_env, domain, urls)

    assert reconcile(domain)["updated"] == 2       # /1 首错后仍是 unprocessed，/2 也是
    assert _stats(domain) == {"skipped": 1, "done": 2}
