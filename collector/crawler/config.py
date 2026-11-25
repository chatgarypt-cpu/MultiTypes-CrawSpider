"""Paths, constants and the shared HTTP user agent."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # collector/
DATA = os.path.abspath(os.environ.get("CRAWL_DATA_DIR", os.path.join(ROOT, "data")))
DB = os.path.join(DATA, "state.db")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MEDIA_COLS = ["id","title","url","link","mime","width","height","alt","caption",
              "description","created_at","parent","host"]

COLS = ["title","sub_title","url","channel_text","country_text","lang_text","user_id","user_author_name","user_url",
        "created_at","description","text","image","video","link","view_count","like_count","reply_count",
        "retweet_count","dislike_count","favourite_count","tag","keyword","host","first_channel","second_channel",
        "third_channel","editor","updated_at","hit_num","read_num"]

WORKERS = 8           # set by the CLI, used by the wp-api channel
REQUEST_QUEUE_CHANNELS = frozenset({"sitemap", "html", "rss", "archive", "json-list"})


def catalog_path(directory=ROOT) -> Path:
    """Writable catalog, optionally stored in the deployment's data volume."""
    override = os.environ.get("CRAWL_SOURCES_FILE")
    return Path(override) if override else Path(directory) / "sources.yaml"


def sources_path(directory=ROOT) -> Path:
    """Use the operator's local catalog, or the empty template on a fresh checkout."""
    root = Path(directory)
    for local in (catalog_path(directory), root / "sources.yaml"):
        if local.exists():
            return local
    return root / "sources.example.yaml"


def seed_catalog(contents=None):
    """Create the writable catalog once, including on an upgraded data volume."""
    target = catalog_path()
    if target.exists():
        return
    contents = sources_path().read_bytes() if contents is None else contents
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent) as staged:
        staged.write(contents)
        staged.flush()
        os.fsync(staged.fileno())
        try:
            os.link(staged.name, target)
        except FileExistsError:
            pass


def site_kinds(site):
    """Configured collections, including explicitly verified public WP types."""
    if site.get("channel") != "wp-api":
        return ("posts",)
    kinds = tuple(dict.fromkeys(("posts", "pages", "media", *(site.get("wp_types") or {}))))
    return tuple(kind for kind in kinds if not site.get("_scope_kinds") or kind in site["_scope_kinds"])


def stored_kinds(directory):
    """Collections present on disk; keep the three historical columns available."""
    from pathlib import Path
    extra = sorted(p.stem for p in Path(directory).glob("*.jsonl")
                   if p.stem not in ("articles", "posts", "pages", "media"))
    return ("posts", "pages", "media", *extra)


def read_collection_skips(path):
    """Read the operator's domain:kind pause list, ignoring comments."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {key for line in lines if (key := line.split("#", 1)[0].strip())}


# ---------- 全局放开开关 ----------
# 采集端默认遵守 robots、默认跳过 sources.yaml 里 disabled 的站。运维需要"全站全量、
# 不按 robots 限流"时，落一个哨兵文件 data/CRAWL_BYPASS（或 CRAWL_BYPASS=1），
# 进程重启后所有出口都按放开模式跑。删除文件即恢复默认，不需要改 sources.yaml。
_TRUTHY = ("1", "true", "yes", "on", "all")
_FALSY = ("0", "false", "no", "off")


def bypass_file() -> str:
    """哨兵文件路径（运行时读 DATA，测试可 patch crawler.config.DATA）。"""
    return os.path.join(DATA, "CRAWL_BYPASS")


def bypass() -> bool:
    """放开 robots / disabled 限制。

    环境变量显式写了 0/false/off 就以它为准（测试用），否则看哨兵文件。
    """
    env = os.environ.get("CRAWL_BYPASS", "").strip().lower()
    if env in _TRUTHY:
        return True
    if env in _FALSY:
        return False
    return os.path.exists(bypass_file())
