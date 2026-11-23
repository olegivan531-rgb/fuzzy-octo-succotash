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


# ---------------------------------------------------------------- fixes after live review

REPLY_MULTIPART = (b"From: Chan <x@y.com>\r\nSubject: Re: shorts\r\nMIME-Version: 1.0\r\n"
                   b"Content-Type: multipart/alternative; boundary=\"b1\"\r\n\r\n"
                   b"--b1\r\nContent-Type: text/plain; charset=UTF-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                   b"U3VyZSwgc2VuZCB0aGVtIQ==\r\n"
                   b"--b1\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n<div>Sure, send them!</div>\r\n--b1--\r\n")

BOUNCE_QP = (b"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\nMIME-Version: 1.0\r\n"
             b"Content-Type: multipart/report; report-type=delivery-status; boundary=\"r\"\r\n\r\n"
             b"--r\r\nContent-Type: text/plain; charset=UTF-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n"
             b"Address not found =E2=80=94 your message wasn't delivered to dead@=\r\nz.com\r\n"
             b"--r\r\nContent-Type: message/delivery-status\r\n\r\n"
             b"Reporting-MTA: dns; googlemail.com\r\n\r\nFinal-Recipient: rfc822; dead@z.com\r\nAction: failed\r\n"
             b"--r--\r\n")


def test_mail_text_decodes_multipart_base64():
    text = ox.mail_text(REPLY_MULTIPART)
    assert "Sure, send them!" in text and "--b1" not in text and "U3Vy" not in text


def test_bounce_found_in_encoded_report():
    s, y = y_state()
    y["ch"] = {"b": {"status": "sent", "email": "dead@z.com", "name": "B", "link": "l"}}
    ox.on_mail(s, "mailer-daemon@googlemail.com", ox.mail_text(BOUNCE_QP), {"dead@z.com": "b"})
    assert y["ch"]["b"]["status"] == "bounced"


def test_reply_quote_cut_and_no_mime_noise():
    s, y = y_state()
    y["ch"] = {"a": {"status": "sent", "email": "x@y.com", "name": "Chan", "link": "l"}}
    body = ox.mail_text(REPLY_MULTIPART) + "\nOn Mon, Nov 24, 2026 at 3:00 PM Oleg <me@gmail.com> wrote:\n> old"
    ox.on_mail(s, "x@y.com", body, {"x@y.com": "a"})
    assert "Sure, send them!" in sent_msgs[-1] and "> old" not in sent_msgs[-1]


def test_three_bounces_pause_once():
    s, y = y_state()
    y["ch"] = {str(i): {"status": "sent", "email": f"d{i}@z.com", "name": str(i), "link": "l"} for i in range(4)}
    contacted = {c["email"]: cid for cid, c in y["ch"].items()}
    before = len(sent_msgs)
    for i in range(4):
        ox.on_mail(s, "mailer-daemon@googlemail.com", f"failed d{i}@z.com", contacted)
    assert y["paused"]
    assert sum("розсилку зупинено" in m for m in sent_msgs[before:]) == 1


def test_recipient_refused_marks_bounced_not_retried(monkeypatch):
    import smtplib
    s, y = y_state()
    y["ch"] = {"a": {"status": "queued", "email": "nobody@z.com", "name": "A", "subject": "s", "body": "b",
                     "subs": 1, "link": "l"}}

    def refuse(*a, **k):
        raise smtplib.SMTPRecipientsRefused({"nobody@z.com": (550, b"no such user")})

    monkeypatch.setattr(ox, "smtp_send", refuse)
    monkeypatch.setattr(ox, "can_send_now", lambda y, now=None: True)
    ox.send_due(s)
    assert y["ch"]["a"]["status"] == "bounced"
    assert ox.pick_next(y, time.time())[0] is None


ATOM_OLD = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
  <id>t3_old</id><title>[Hiring] Shorts editor</title><link href="https://reddit.com/1"/>
  <author><name>/u/a</name></author><content type="html">old</content></entry></feed>"""


def test_reddit_first_read_is_per_subreddit(monkeypatch):
    s = {}
    calls = []
    fail = {"hiring"}

    def feed(path):
        sub = path.split("/")[2]
        calls.append(sub)
        if sub in fail:
            raise RuntimeError("www.reddit.com: HTTP 403")
        return ATOM_OLD.replace(b"t3_old", f"t3_{sub}".encode())

    monkeypatch.setattr(ox, "reddit_feed", feed)
    before = len(sent_msgs)
    ox.scan_reddit(s)                 # first read: remember only, "hiring" failed
    assert len(sent_msgs) == before and "hiring" not in s["rd"]["subs_init"]
    fail.clear()
    ox.scan_reddit(s)                 # "hiring" read for the first time: still nothing sent
    assert len(sent_msgs) == before and "hiring" in s["rd"]["subs_init"]


def test_reddit_feed_reports_all_statuses_and_uses_proxy(monkeypatch):
    import urllib.error
    tried = []

    def get(url):
        tried.append(url)
        if "proxy" in url:
            return b"<feed/>"
        raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)

    monkeypatch.setattr(ox, "_get", get)
    monkeypatch.setattr(ox, "REDDIT_RSS_PROXY", "")
    try:
        ox.reddit_feed("/r/forhire/new/.rss")
        assert False
    except RuntimeError as e:
        assert "www.reddit.com: HTTP 403" in str(e) and "old.reddit.com: HTTP 403" in str(e)
    monkeypatch.setattr(ox, "REDDIT_RSS_PROXY", "https://proxy.example/?u={url}")
    assert ox.reddit_feed("/r/forhire/new/.rss") == b"<feed/>"
    assert tried[-1].startswith("https://proxy.example/?u=https%3A%2F%2Fwww.reddit.com")


def test_skipped_channels_are_trimmed(monkeypatch):
    monkeypatch.setattr(ox, "MAX_SKIPPED", 3)
    y = {"ch": {f"s{i}": {"status": "skip", "why": "subs"} for i in range(6)}}
    y["ch"]["real"] = {"status": "skip", "email": "keep@me.com"}     # a real contact is never forgotten
    ox._trim_skipped(y)
    assert list(y["ch"]) == ["s3", "s4", "s5", "real"]


def test_tick_blocks_never_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    logged = []
    ox.init(log=lambda *a: logged.append(a))
    for name in ("discover", "scan_reddit", "send_due", "check_replies", "check_reddit_inbox"):
        monkeypatch.setattr(ox, name, boom)
    monkeypatch.setattr(ox, "YT_ON", True)
    monkeypatch.setattr(ox, "RD_ON", True)
    s = {}
    ox.tick_scan(s)
    ox.tick_track(s)
    assert len(logged) == 5
    ox.init(log=lambda *a: None)


class FakeIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in: uid search/fetch over a dict of raw messages."""
    msgs = {}

    def __init__(self, *a, **k):
        self.fetched_full = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def select(self, *a, **k):
        pass

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [" ".join(str(u) for u in self.msgs).encode()]
        uid, what = int(args[0]), args[1]
        raw = self.msgs[uid]
        if "HEADER.FIELDS" in what:
            frm = [l for l in raw.split(b"\r\n") if l.lower().startswith(b"from:")][0]
            return "OK", [(f"{uid} (RFC822.SIZE {len(raw)} BODY[HEADER.FIELDS (FROM)] {{9}}".encode(),
                           frm + b"\r\n\r\n"), b")"]
        FakeIMAP.full.append(uid)
        return "OK", [(f"{uid} (BODY[] {{{len(raw)}}}".encode(), raw), b")"]


def test_check_replies_end_to_end(monkeypatch):
    FakeIMAP.msgs = {5: b"From: stranger@x.com\r\nSubject: hi\r\n\r\nspam", 6: REPLY_MULTIPART}
    FakeIMAP.full = []
    monkeypatch.setattr(ox.imaplib, "IMAP4_SSL", FakeIMAP)
    s, y = y_state()
    y["ch"] = {"a": {"status": "sent", "email": "x@y.com", "name": "Chan", "link": "l"}}
    ox.check_replies(s)
    assert FakeIMAP.full == [6]                      # only the contacted sender is downloaded in full
    assert y["ch"]["a"]["status"] == "replied" and y["imap_uid"] == 6
    assert "Sure, send them!" in sent_msgs[-1] and "--b1" not in sent_msgs[-1]
    ox.check_replies(s)                              # nothing new after the last UID
    assert FakeIMAP.full == [6]
