from types import SimpleNamespace

import bot


def test_truncate_below_limit():
    assert bot.truncate("hello", 100) == "hello"


def test_truncate_at_limit():
    text = "a" * 4000
    assert bot.truncate(text, 4000) == text


def test_truncate_over_limit_appends_ellipsis():
    text = "a" * 4001
    result = bot.truncate(text, 4000)
    assert len(result) == 4000
    assert result.endswith("…")
    assert result == "a" * 3999 + "…"


def _att(content_type=None, filename="x", url="http://example.com/x"):
    return SimpleNamespace(content_type=content_type, filename=filename, url=url)


def test_pick_image_by_content_type():
    a = _att(content_type="image/png", filename="x.bin")
    assert bot._pick_image_attachment([a]) is a


def test_pick_image_by_extension_when_content_type_missing():
    a = _att(content_type=None, filename="photo.JPG")
    assert bot._pick_image_attachment([a]) is a


def test_pick_image_skips_non_image():
    a = _att(content_type=None, filename="doc.txt")
    assert bot._pick_image_attachment([a]) is None


def test_pick_image_returns_first_match():
    txt = _att(content_type=None, filename="doc.txt", url="http://t")
    img1 = _att(content_type="image/png", filename="a.png", url="http://1")
    img2 = _att(content_type="image/jpeg", filename="b.jpg", url="http://2")
    assert bot._pick_image_attachment([txt, img1, img2]) is img1


def test_pick_image_empty():
    assert bot._pick_image_attachment([]) is None
