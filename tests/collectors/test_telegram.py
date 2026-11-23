import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from osint.collectors.telegram import (
    TelegramCollector,
    html_text,
    page_ids,
    parse_page,
    post_to_document,
)
from osint.core.collector import CollectorContext
from osint.store.sqlite import SqliteStore


def msg(channel: str, mid: int, text: str | None, ts: str = "2026-11-22T21:45:15+00:00",
        fwd: str = "") -> str:
    """One post in the shape of the t.me/s/ preview markup (trimmed)."""
    body = (f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
            if text is not None else '<a class="tgme_widget_message_photo_wrap"></a>')
    fwd_html = (f'<div class="tgme_widget_message_forwarded_from accent_color">Forwarded from '
                f'<a class="tgme_widget_message_forwarded_from_name" href="https://t.me/x">'
                f'<span dir="auto">{fwd}</span></a></div>' if fwd else "")
    return (
        f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="tgme_widget_message text_not_supported_wrap js-widget_message" '
        f'data-post="{channel}/{mid}" data-view="x">'
        f'<div class="tgme_widget_message_bubble">{fwd_html}'
        f'<div class="tgme_widget_message_reply"><div class="tgme_widget_message_text '
        f'js-message_reply_text" dir="auto">quoted reply</div></div>'
        f'<div class="media_supported_cont">{body}</div>'
        f'<div class="tgme_widget_message_footer compact js-message_footer">'
        f'<div class="tgme_widget_message_info short js-message_info">'
        f'<span class="tgme_widget_message_views">62K</span><span class="copyonly"> views</span>'
        f'<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" '
        f'href="https://t.me/{channel}/{mid}"><time datetime="{ts}" class="time">21:45</time>'
        f'</a></span></div></div></div></div></div>')


def page(channel: str, *posts: str) -> str:
    return (f'<html><body><section class="tgme_channel_history js-message_history">'
            f'{"".join(posts)}</section></body></html>')


TEXT = ('<b><i class="emoji" style="x"><b>🔄</b></i></b><b> Мапу оновлено<br/></b><br/>'
        'Ворог просунувся поблизу <a href="https://deepstatemap.live/#14/47.7/36.1">'
        'Верхньої Терси</a> &amp; Устинівки.')


def _ctx(http, tmp_path):
    return CollectorContext(http=http, settings=SimpleNamespace(),
                            store=SqliteStore(tmp_path / "s.sqlite"))


def test_html_text():
    assert html_text(TEXT) == "🔄 Мапу оновлено\nВорог просунувся поблизу Верхньої Терси & Устинівки."


def test_parse_page():
    p = page("DeepStateUA", msg("DeepStateUA", 11, "second"), msg("DeepStateUA", 10, TEXT),
             msg("DeepStateUA", 12, None), msg("OtherChan", 13, "foreign"),
             msg("DeepStateUA", 14, "fwd post", fwd="Some &amp; Channel"))
    posts = parse_page(p, "deepstateua")
    assert [x["id"] for x in posts] == [10, 11, 14]  # sorted; media-only + foreign skipped
    assert posts[0]["text"].startswith("🔄 Мапу оновлено\n")
    assert posts[0]["published"] == datetime(2026, 11, 22, 21, 45, 15, tzinfo=UTC)
    assert posts[0]["views"] == "62K" and posts[2]["forwarded_from"] == "Some & Channel"
    assert "quoted reply" not in posts[1]["text"]
    assert page_ids(p, "DeepStateUA") == [10, 11, 12, 14]


def test_post_to_document():
    (post,) = parse_page(page("DeepStateUA", msg("DeepStateUA", 10, TEXT)), "DeepStateUA")
    now = datetime(2026, 11, 23, tzinfo=UTC)
    d = post_to_document(post, {"name": "DeepStateUA", "language": "uk", "country": "UA"}, now)
    assert d.url == "https://t.me/DeepStateUA/10" and d.source == "telegram:DeepStateUA"
    assert d.title == "🔄 Мапу оновлено" and d.kind == "article" and d.language == "uk"
    assert "Верхньої Терси" in d.snippet and len(d.snippet) <= 300
    assert d.attrs["platform"] == "telegram" and d.origin_id == "DeepStateUA/10"


@respx.mock
async def test_poll_incremental_with_catch_up_and_mirror(tmp_path, caplog):
    base = "https://telegram.me/s"
    first = page("chanA", *(msg("chanA", i, f"post {i}") for i in range(10, 13)))
    later = page("chanA", *(msg("chanA", i, f"post {i}") for i in range(20, 23)))
    older = page("chanA", *(msg("chanA", i, f"post {i}") for i in range(13, 20)))
    back = respx.get(f"{base}/chanA", params={"before": "20"}).mock(
        return_value=httpx.Response(200, text=older))
    top = respx.get(f"{base}/chanA").mock(side_effect=[
        httpx.Response(200, text=first), httpx.Response(200, text=later)])
    # a private/renamed channel redirects to the plain channel page (no message history)
    respx.get(f"{base}/gone_chan").mock(return_value=httpx.Response(
        200, text="<html><div class='tgme_page'></div></html>"))
    c = TelegramCollector({"base_url": "https://telegram.me", "min_interval_s": 0,
                           "channels": [{"name": "chanA", "language": "en"}, "gone_chan",
                                        {"name": "bad name!"}]})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http, tmp_path)
        with caplog.at_level(logging.WARNING):
            b1 = await c.poll(ctx)
        assert [d.url for d in b1.documents] == [f"https://t.me/chanA/{i}" for i in (10, 11, 12)]
        assert ctx.get_cursor("telegram")["channels"]["chanA"]["last_id"] == 12
        b2 = await c.poll(ctx)
    # second poll: 20..22 is not contiguous with 12, so one older page (13..19) is read
    assert [int(d.url.rsplit("/", 1)[1]) for d in b2.documents] == list(range(13, 23))
    assert top.call_count == 2 and back.call_count == 1
    assert ctx.get_cursor("telegram")["channels"]["chanA"]["last_id"] == 22
    assert ctx.get_cursor("telegram")["failed"] == 1
    assert "gone_chan: RuntimeError: no public web preview" in caplog.text
    assert "invalid channel entry" in caplog.text


@respx.mock
async def test_poll_raises_when_all_channels_fail(tmp_path):
    respx.get("https://t.me/s/chanA").mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="all Telegram channels failed"):
            await TelegramCollector({"channels": ["chanA"]}).poll(_ctx(http, tmp_path))
        with pytest.raises(RuntimeError, match="no channels"):
            await TelegramCollector({}).poll(_ctx(http, tmp_path))


def test_repo_channel_list_is_well_formed():
    from osint.collectors.telegram import _CHANNEL
    from osint.config import ROOT, load_sources

    chans = load_sources(ROOT / "config")["telegram"]["channels"]
    assert len(chans) >= 20
    assert all(_CHANNEL.match(c["name"]) and c["language"] for c in chans)
    assert len({c["name"].casefold() for c in chans}) == len(chans)
