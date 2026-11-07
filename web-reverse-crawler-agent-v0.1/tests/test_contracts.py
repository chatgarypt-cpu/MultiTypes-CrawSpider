from wrc.collect import collect
from wrc.site import ARTICLES
import pytest

def test_fixture():
    assert len(ARTICLES) == 12
    assert all(set(["id", "title", "url", "published_at"]) <= set(row) for row in ARTICLES)

def test_reject_unapproved_host(tmp_path):
    with pytest.raises(ValueError):
        collect("https://example.com/api/articles", tmp_path / "a.db", tmp_path / "a.csv")
