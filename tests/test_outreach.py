"""Outreach filters, limits and parsing. No network, no secrets."""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import outreach as ox  # noqa: E402

sent_msgs = []
ox.init(send=lambda *a, **k: sent_msgs.append(a[0]) or {"message_id": 1}, edit_buttons=lambda *a: None,
        ai_text=lambda *a, **k: None, log=lambda *a: None, http_json=None)
NOW = datetime.now(timezone.utc)


def ch(subs=20000, desc="Business: team@creator.com"):
    return {"statistics": {"subscriberCount": str(subs)}, "snippet": {"description": desc}}


def up(secs, days_ago=3):
    return {"title": "v", "secs": secs, "published": NOW - timedelta(days=days_ago)}


def test_iso_duration():
    assert ox.iso_dur("PT1H2M3S") == 3723 and ox.iso_dur("PT45S") == 45 and ox.iso_dur("P0D") == 0


def test_email_found_and_images_ignored():
    assert ox.find_email("logo@2x.png contact: Hello@Brand.co.uk.") == "hello@brand.co.uk"
    assert ox.find_email("no mail here") is None


def test_fit_good_channel():
    assert ox.channel_fit(ch(), [up(1500)] + [up(900, 10)] * 5)[0]


def test_fit_rejects():
    assert ox.channel_fit(ch(subs=100), [up(1500)])[1] == "subs"
    assert ox.channel_fit(ch(desc="no contact"), [up(1500)])[1] == "no email"
    assert ox.channel_fit(ch(), [up(30)] * 5 + [up(1500)])[1] == "already does shorts"
    assert ox.channel_fit(ch(), [up(1500, 90)])[1] == "inactive"


def y_state():
    s = {}
    return s, ox._state(s)


def test_send_window_and_caps():
    s, y = y_state()
    tue_noon_ny = datetime(2026, 11, 24, 16, tzinfo=timezone.utc)
    assert ox.can_send_now(y, time.time(), tue_noon_ny)
    assert not ox.can_send_now(y, time.time(), datetime(2026, 11, 28, 16, tzinfo=timezone.utc))  # Saturday
    assert not ox.can_send_now(y, time.time(), datetime(2026, 11, 24, 3, tzinfo=timezone.utc))   # night in US
    y["sent_log"] = [time.time()] * ox.DAILY_CAP
    assert not ox.can_send_now(y, time.time(), tue_noon_ny)
    y["sent_log"], y["last_sent"], y["next_gap"] = [], time.time(), 1800
    assert not ox.can_send_now(y, time.time(), tue_noon_ny)
    y["paused"], y["next_gap"] = True, 0
    assert not ox.can_send_now(y, time.time(), tue_noon_ny)


def test_followup_only_after_days():
    s, y = y_state()
    now = time.time()
    y["ch"] = {"a": {"status": "sent", "sent_at": now - 86400}}
    assert ox.pick_next(y, now)[1] is None
    y["ch"]["a"]["sent_at"] = now - 5 * 86400
    assert ox.pick_next(y, now)[2] is True


def test_reply_and_bounce():
    s, y = y_state()
    y["ch"] = {"a": {"status": "sent", "email": "x@y.com", "name": "Chan", "link": "l"},
               "b": {"status": "sent", "email": "dead@z.com", "name": "B", "link": "l"}}
    contacted = {"x@y.com": "a", "dead@z.com": "b"}
    ox.on_mail(s, "x@y.com", "Sure, send them!\nOn Mon, Nov 24 Oleg wrote: old", contacted)
    assert y["ch"]["a"]["status"] == "replied" and "Sure, send them!" in sent_msgs[-1]
    ox.on_mail(s, "mailer-daemon@googlemail.com", "Delivery to dead@z.com failed", contacted)
    assert y["ch"]["b"]["status"] == "bounced"


def test_reddit_lead_filter_and_atom():
    raw = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>t3_1</id><title>[Hiring] Shorts editor for podcast</title>
      <link href="https://reddit.com/r/forhire/1"/><author><name>/u/bob</name></author>
      <content type="html">&lt;p&gt;Need 10 clips a week&lt;/p&gt;</content></entry></feed>"""
    p = ox.parse_atom(raw)[0]
    assert p["author"] == "bob" and p["text"] == "Need 10 clips a week"
    assert ox.is_reddit_lead(p)
    assert not ox.is_reddit_lead({"title": "[For Hire] video editor", "text": ""})
    assert not ox.is_reddit_lead({"title": "[Hiring] React developer", "text": "web app"})
    assert "to=bob" in ox.compose_link("bob", "Re: x", "hi there")


def test_commands():
    s, y = y_state()
    assert ox.handle_command(s, "/pause") and y["paused"]
    assert ox.handle_command(s, "/go") and not y["paused"]
    assert not ox.handle_command(s, "hello")
