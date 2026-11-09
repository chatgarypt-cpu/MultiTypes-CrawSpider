"""text.clean：WP 页面构建器短代码不能混进正文（2026-11 在 wp-api 站发现）。"""
from crawler.text import clean


def test_wpbakery_shortcodes_removed():
    raw = ("[vc_row][vc_column][vc_column_text]In the month of March, "
           "PCS welcomed a new partner.[/vc_column_text][/vc_column][/vc_row]")
    assert clean(raw) == "In the month of March, PCS welcomed a new partner."


def test_other_builders_removed():
    raw = '[et_pb_section][fusion_text]Hello[/fusion_text][/et_pb_section][su_note]x[/su_note]'
    assert "et_pb" not in clean(raw) and "fusion" not in clean(raw) and "su_note" not in clean(raw)
    assert "Hello" in clean(raw)


def test_plain_brackets_kept():
    assert clean("[Photo: reef] text") == "[Photo: reef] text"
