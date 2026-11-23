"""Live self-check of outreach.py against the real services (run from GitHub Actions: outreach-check).

Never contacts strangers and never touches Telegram:
- YouTube: one search query -> channels -> AI drafts, printed here (emails masked), quota counted.
- Gmail: one test email to GMAIL_USER itself, then IMAP login + check_replies must see it.
- Reddit: every watched subreddit feed (and REDDIT_INBOX_RSS if set): HTTP status and entry count.
Exit code 1 if a configured part fails.
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fh_bot as bot  # noqa: E402
import outreach as ox  # noqa: E402

EMAIL = re.compile(r"([\w.+-])[\w.+-]*@([\w-])[\w.-]*")
report, failed = [], []


def mask(t):
    return EMAIL.sub(r"\1***@\2***", str(t))


def out(line=""):
    print(mask(line), flush=True)
    report.append(mask(line))


def fake_send(text, *a, **k):
    out("----- would go to Telegram -----\n" + text)
    return {"message_id": 0}


ox.init(send=fake_send, edit_buttons=lambda *a, **k: None, ai_text=bot.ai_text, log=lambda *a: out(" ".join(map(str, a))),
        http_json=bot.http_json)

# ---------------------------------------------------------------- YouTube
units = {"n": 0}
COST = {"search": 100}
_yt = ox.yt


def yt_counted(path, **params):
    units["n"] += COST.get(path, 1)
    return _yt(path, **params)


def check_youtube():
    if not ox.YT_API_KEY:
        out("YouTube: skipped (no YT_API_KEY)")
        return
    ox.yt = yt_counted
    ox.DRAFTS_PER_DAY = 3          # a handful of AI drafts is enough to judge
    ox.AUTO = False
    s = {}
    try:
        ox.discover(s)
    except Exception as e:
        failed.append("youtube")
        out(f"YouTube: ❌ {e}")
        return
    y = s["yt"]
    drafts = [c for c in y["ch"].values() if c.get("status") == "draft"]
    reasons = {}
    for c in y["ch"].values():
        if c.get("status") == "skip":
            reasons[c.get("why")] = reasons.get(c.get("why"), 0) + 1
    out(f"YouTube: ✅ query '{ox.YT_QUERIES[0]}', channels checked {len(y['ch'])}, drafts {len(drafts)}, "
        f"skipped {reasons}, quota used ~{units['n']} units of 10000/day")
    if not drafts:
        out("YouTube: ⚠️ no drafts from this query (normal for some queries; the bot rotates 12 of them)")


# ---------------------------------------------------------------- Gmail
def check_gmail():
    if not (ox.GMAIL_USER and ox.GMAIL_APP_PASSWORD):
        out("Gmail: skipped (no GMAIL_USER / GMAIL_APP_PASSWORD)")
        return
    me = ox.GMAIL_USER.lower()
    s = {}
    y = ox._state(s)
    try:
        import imaplib
        with imaplib.IMAP4_SSL("imap.gmail.com", 993) as im:      # start after the newest existing message
            im.login(ox.GMAIL_USER, ox.GMAIL_APP_PASSWORD)
            im.select("INBOX", readonly=True)
            _, data = im.uid("search", None, "ALL")
            uids = [int(u) for u in (data[0] or b"").split()]
            y["imap_uid"] = max(uids) if uids else 0
        out("Gmail IMAP: ✅ login ok")
    except Exception as e:
        failed.append("imap")
        out(f"Gmail IMAP: ❌ {e} (IMAP must be on: Gmail → Settings → Forwarding and POP/IMAP)")
        return
    tag = f"outreach self-check {int(time.time())}"
    try:
        msgid = ox.smtp_send(ox.GMAIL_USER, tag, "Test from the Freelancehunt bot outreach self-check. Ignore it.")
        out(f"Gmail SMTP: ✅ test email sent to yourself ({msgid})")
    except Exception as e:
        failed.append("smtp")
        out(f"Gmail SMTP: ❌ {e}")
        return
    y["ch"] = {"self": {"status": "sent", "email": me, "name": "self-check", "link": "-"}}
    for _ in range(9):
        time.sleep(10)
        ox.check_replies(s)
        if y["ch"]["self"]["status"] == "replied":
            out("Gmail IMAP: ✅ check_replies found the test email and parsed it")
            return
    out("Gmail IMAP: ⚠️ test email not seen in INBOX within 90 s (Gmail sometimes files mail to yourself "
        "only under Sent); login and search work")


# ---------------------------------------------------------------- Reddit
def check_reddit():
    if not ox.RD_ON:
        out("Reddit: skipped (REDDIT_ENABLED=0)")
        return
    ok = 0
    for sub in ox.RD_SUBS:
        try:
            n = len(ox.parse_atom(ox.reddit_feed(f"/r/{sub}/new/.rss?limit=50")))
            out(f"Reddit r/{sub}: ✅ {n} posts")
            ok += 1
        except Exception as e:
            out(f"Reddit r/{sub}: ❌ {e}")
    if not ok:
        failed.append("reddit")
        out("Reddit: ❌ no feed reachable from here. Set REDDIT_RSS_PROXY, or run the bot with "
            "REDDIT_ENABLED=1 on your own machine and REDDIT_ENABLED=0 in Actions (see README).")
    if ox.REDDIT_INBOX_RSS:
        try:
            n = len(ox.parse_atom(ox._get(ox.REDDIT_INBOX_RSS)))
            out(f"Reddit inbox RSS: ✅ {n} messages")
        except Exception as e:
            failed.append("reddit inbox")
            out(f"Reddit inbox RSS: ❌ {type(e).__name__} {getattr(e, 'code', '')}")


if __name__ == "__main__":
    for part in (check_youtube, check_gmail, check_reddit):
        out(f"\n=== {part.__name__} ===")
        try:
            part()
        except Exception as e:
            failed.append(part.__name__)
            out(f"{part.__name__}: ❌ unexpected {type(e).__name__}: {e}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write("```\n" + "\n".join(report) + "\n```\n")
    out(f"\nFAILED: {failed}" if failed else "\nALL CONFIGURED PARTS OK")
    sys.exit(1 if failed else 0)
