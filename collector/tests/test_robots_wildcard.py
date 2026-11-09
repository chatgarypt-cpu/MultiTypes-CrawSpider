"""robots 通配规则回归（croix-rouge/noaa 事故：`/*search=*` 曾把全站判成禁止）。"""
from crawler.politeness import _allowed_path, parse_robots


RULES = [("allow", "/"), ("disallow", "/*search=*"), ("disallow", "/*sort=*"),
         ("disallow", "/campagnes/")]


def test_plain_path_not_blocked_by_early_wildcard_rule():
    assert _allowed_path("/abri-de-nuit-apaso", RULES) is True
    assert _allowed_path("/actualites/x", RULES) is True


def test_wildcard_rule_only_matches_its_substring():
    assert _allowed_path("/actualites/x?search=a", RULES) is False
    assert _allowed_path("/campagnes/y", RULES) is False


def test_suffix_wildcard_matches_any_prefix():
    rules = [("disallow", "/*/media/oembed"), ("disallow", "/core/")]
    assert _allowed_path("/pacific-islands/about-us/x", rules) is True
    assert _allowed_path("/foo/media/oembed", rules) is False
    assert _allowed_path("/core/app.js", rules) is False


def test_dollar_anchor_limits_to_path_end():
    rules = [("disallow", "/foo.html$")]
    assert _allowed_path("/foo.html", rules) is False
    assert _allowed_path("/foo.html.bak", rules) is True


def test_parse_robots_only_star_group_and_crawl_delay():
    text = """
    User-agent: GPTBot
    Disallow: /

    User-agent: *
    Disallow: /admin/
    Crawl-delay: 2.5
    """
    rules, delay = parse_robots(text)
    assert ("disallow", "/") not in rules          # GPTBot 组不生效
    assert ("disallow", "/admin/") in rules
    assert delay == 2.5
    assert _allowed_path("/article/1", rules) is True
    assert _allowed_path("/admin/x", rules) is False


def test_allow_wins_on_equal_specificity():
    rules = [("allow", "/news/"), ("disallow", "/news/")]
    assert _allowed_path("/news/a", rules) is True
