"""Outreach for short-form video editing (Shorts/Reels/TikTok).

1) YouTube: finds active long-form channels that barely post Shorts and show a business
   email in their description, AI writes a personal email, sends it from Gmail with strict
   limits (daily cap, gap between emails, US working hours, weekdays, one follow-up),
   then watches the Gmail inbox and pings Telegram when someone replies.
2) Reddit: watches [Hiring] posts about video editing, AI drafts a DM; you send it with one
   tap (prefilled compose link). Messaging strangers automatically gets Reddit accounts
   banned, so the bot never sends on Reddit itself. Your Reddit inbox (private RSS) is
   watched too, and replies come to Telegram.

Env (each part turns on only when its keys are set):
  YT_API_KEY, GMAIL_USER, GMAIL_APP_PASSWORD, PORTFOLIO_URL
  OUTREACH_AUTO=1   send without asking (default 0: each email waits for your tap)
  OUTREACH_DAILY    emails per day, follow-ups included (default 12)
  REDDIT_INBOX_RSS  private inbox feed URL from reddit.com/prefs/feeds (optional)
"""
import email.utils, html, imaplib, json, random, re, smtplib, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
import os


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


YT_API_KEY = env("YT_API_KEY")
GMAIL_USER = env("GMAIL_USER")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD").replace(" ", "")
PORTFOLIO_URL = env("PORTFOLIO_URL")
REDDIT_INBOX_RSS = env("REDDIT_INBOX_RSS")
AUTO = env("OUTREACH_AUTO", "0") == "1"
DAILY_CAP = int(env("OUTREACH_DAILY", "12") or 12)

YT_ON = bool(YT_API_KEY and GMAIL_USER and GMAIL_APP_PASSWORD)
RD_ON = env("REDDIT_ENABLED", "1") == "1"

# ----- limits (anti-spam) -----
MIN_GAP_MIN = 25            # minutes between two emails (+ random 0-20)
SEND_HOURS_UTC = (13, 21)   # 9:00-17:00 New York
FOLLOWUP_AFTER_D = 4        # one short follow-up if no reply
DRAFTS_PER_DAY = 15         # how many new channels to prepare per day
BOUNCE_PAUSE = 3            # pause sending after this many bounces in 24 h

# ----- who to look for -----
YT_QUERIES = ["podcast interview", "business advice", "personal finance", "real estate investing",
              "fitness coaching", "marketing tips", "self improvement", "startup founder",
              "sales training", "investing explained", "productivity", "coaching"]
SUBS_MIN, SUBS_MAX = 5_000, 500_000
MAX_SHORTS_OF_20 = 2        # channel already posting Shorts -> not our client
ACTIVE_DAYS = 45            # last long video not older than this

RD_SUBS = ["forhire", "hiring", "jobbit", "VideoEditingRequests", "slavelabour"]
RD_TOPIC = re.compile(r"video ?edit|editor|shorts|reels|tiktok|youtube|capcut|premiere|davinci|after effects", re.I)
RD_HIRING = re.compile(r"\[hiring\]|\bhiring\b|looking for|need (an? )?(video )?editor|paid", re.I)
RD_NOT = re.compile(r"\[for hire\]|\bfor hire\b", re.I)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

SERVICE = """Oleg — short-form video editor (YouTube Shorts, Reels, TikTok).
Cuts long videos (podcasts, interviews, talking-head) into vertical clips: strong hook in the first
2 seconds, animated captions, zooms/b-roll, sound design, 9:16 reframing. Turnaround 24-48 h.
Offers 2 free test Shorts from the client's own video, no obligation."""

EMAIL_PROMPT = """Write a short cold email from a video editor to a YouTube creator.

Editor:
{service}
Portfolio: {portfolio}

Channel: {name} ({subs} subscribers)
About: {about}
Recent long videos: {titles}
Shorts in last 20 uploads: {shorts}

Rules: English, 60-100 words, plain text, no emojis, no flattery clichés, no "I hope this finds you well".
First sentence is about ONE specific recent video by title and which moment would make a good Short.
Offer 2 free Shorts cut from that video. Give the portfolio link. Sign as Oleg.
Last line exactly: "If it's not relevant, just reply 'no' and I won't write again."
Subject: 2-6 words, lowercase, specific to the channel, not salesy.
Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

FOLLOWUP_PROMPT = """Write a 2-3 sentence follow-up (English, no emojis) to this unanswered email.
Do not repeat it; just offer once more to send 2 free Shorts and ask a yes/no question. Sign as Oleg.
Original: {body}
Return ONLY the text."""

REDDIT_PROMPT = """Write a Reddit private message replying to this hiring post.
Me:
{service}
Portfolio: {portfolio}
Post title: {title}
Post: {text}
Rules: English, 40-80 words, casual and direct, refer to their specific needs, offer a free test edit,
give the portfolio link, no emojis, no rates unless the post asks. Return ONLY the message text."""

_f = {}   # functions from the main bot: send, edit_buttons, ai_text, log, http_json


def init(**fns):
    _f.update(fns)


def log(*a):
    _f["log"](*a)


def send(*a, **k):
    return _f["send"](*a, **k)


def _state(s):
    s.setdefault("yt", {})
    y = s["yt"]
    for k, v in {"ch": {}, "q": 0, "sent_log": [], "drafted_log": [], "last_sent": 0,
                 "next_gap": 0, "paused": False, "imap_uid": 0, "bounces": []}.items():
        y.setdefault(k, v)
    s.setdefault("rd", {})
    for k, v in {"seen": [], "inbox_seen": [], "inbox_init": False}.items():
        s["rd"].setdefault(k, v)
    return y


def _json_obj(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON")
    r = json.loads(m.group(0))
    if not r.get("subject") or not r.get("body"):
        raise ValueError("JSON without subject/body")
    return r


def _24h(lst, now):
    return [t for t in lst if now - t < 86400]

# ================================================================= YouTube discovery

def yt(path, **params):
    params["key"] = YT_API_KEY
    return _f["http_json"](f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode(params))


def iso_dur(d):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    dd, h, mi, se = (int(x or 0) for x in m.groups())
    return dd * 86400 + h * 3600 + mi * 60 + se


def find_email(text):
    for e in EMAIL_RE.findall(text or ""):
        e = e.strip(".").lower()
        if not re.search(r"\.(png|jpe?g|gif|webp)$", e) and "example" not in e:
            return e
    return None


def channel_fit(ch, uploads):
    """uploads: [{"title", "secs", "published"}] newest first. Returns (ok, reason)."""
    st = ch.get("statistics", {})
    if st.get("hiddenSubscriberCount"):
        return False, "hidden subs"
    subs = int(st.get("subscriberCount") or 0)
    if not SUBS_MIN <= subs <= SUBS_MAX:
        return False, "subs"
    if not find_email(ch.get("snippet", {}).get("description", "")):
        return False, "no email"
    shorts = sum(1 for u in uploads if u["secs"] and u["secs"] <= 60)
    if shorts > MAX_SHORTS_OF_20:
        return False, "already does shorts"
    longs = [u for u in uploads if u["secs"] >= 8 * 60]
    if not longs:
        return False, "no long videos"
    age = datetime.now(timezone.utc) - longs[0]["published"]
    if age > timedelta(days=ACTIVE_DAYS):
        return False, "inactive"
    return True, ""


def discover(s):
    """One search query per call; prepares drafts for fitting channels."""
    y = _state(s)
    now = time.time()
    y["drafted_log"] = _24h(y["drafted_log"], now)
    if len(y["drafted_log"]) >= DRAFTS_PER_DAY:
        return
    q = YT_QUERIES[y["q"] % len(YT_QUERIES)]
    y["q"] += 1
    after = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = yt("search", part="snippet", type="video", q=q, videoDuration="long", order="relevance",
             publishedAfter=after, relevanceLanguage="en", regionCode="US", maxResults=50)
    ids = []
    for it in res.get("items", []):
        cid = it["snippet"]["channelId"]
        if cid not in y["ch"] and cid not in ids:
            ids.append(cid)
    if not ids:
        return
    chans = yt("channels", part="snippet,statistics,contentDetails", id=",".join(ids[:50])).get("items", [])
    made = skipped = 0
    for ch in chans:
        cid = ch["id"]
        sn = ch.get("snippet", {})
        if not find_email(sn.get("description", "")):
            y["ch"][cid] = {"status": "skip", "why": "no email"}
            skipped += 1
            continue
        try:
            uploads = recent_uploads(ch)
        except Exception as e:
            log("yt uploads error", cid, e)
            continue
        ok, why = channel_fit(ch, uploads)
        if not ok:
            y["ch"][cid] = {"status": "skip", "why": why}
            skipped += 1
            continue
        if len(y["drafted_log"]) >= DRAFTS_PER_DAY:
            break
        c = {"name": sn.get("title", ""), "email": find_email(sn.get("description", "")),
             "subs": int(ch["statistics"].get("subscriberCount") or 0),
             "about": re.sub(r"\s+", " ", sn.get("description", ""))[:600],
             "titles": [u["title"] for u in uploads if u["secs"] >= 8 * 60][:5],
             "shorts": sum(1 for u in uploads if u["secs"] and u["secs"] <= 60),
             "link": f"https://www.youtube.com/channel/{cid}"}
        if any(x.get("email") == c["email"] for x in y["ch"].values()):
            y["ch"][cid] = {"status": "skip", "why": "duplicate email"}
            continue
        if not write_email(c):
            continue
        y["ch"][cid] = c
        y["drafted_log"].append(now)
        made += 1
        if AUTO:
            c["status"] = "queued"
        else:
            c["status"] = "draft"
            show_draft(cid, c)
    log(f"youtube '{q}': {len(chans)} channels, {made} drafts, {skipped} skipped")


def recent_uploads(ch):
    pl = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not pl:
        return []
    items = yt("playlistItems", part="contentDetails", playlistId=pl, maxResults=20).get("items", [])
    vids = [i["contentDetails"]["videoId"] for i in items]
    if not vids:
        return []
    out = []
    for v in yt("videos", part="snippet,contentDetails", id=",".join(vids)).get("items", []):
        out.append({"title": v["snippet"]["title"], "secs": iso_dur(v["contentDetails"].get("duration")),
                    "published": datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))})
    out.sort(key=lambda u: u["published"], reverse=True)
    return out


def write_email(c):
    r = _f["ai_text"](EMAIL_PROMPT.format(service=SERVICE, portfolio=PORTFOLIO_URL or "(none yet)",
                                          name=c["name"], subs=c["subs"], about=c["about"],
                                          titles="; ".join(c["titles"]), shorts=c["shorts"]),
                      parse=_json_obj)
    if not r:
        return False
    c["subject"], c["body"] = r["subject"].strip(), r["body"].strip()
    return True

# ================================================================= Telegram

def draft_text(c):
    return (f"🎬 YouTube: {c['name']} · {c['subs']:,} підп. · Shorts {c['shorts']}/20\n"
            f"📧 {c['email']}\n🔗 {c['link']}\n\nТема: {c['subject']}\n\n{c['body']}")


def draft_buttons(cid):
    return [[{"text": "📤 Надіслати", "callback_data": f"ys:{cid}"},
             {"text": "✏️ Переписати", "callback_data": f"yr:{cid}"},
             {"text": "❌ Пропустити", "callback_data": f"yx:{cid}"}]]


def show_draft(cid, c):
    msg = send(draft_text(c), draft_buttons(cid))
    c["msg_id"] = msg["message_id"] if msg else None


ACTIONS = ("ys", "yr", "yx")


def handle_callback(s, action, cid):
    y = _state(s)
    c = y["ch"].get(cid)
    if not c or "email" not in c:
        send("Цей канал уже не відстежується.")
        return
    _f["edit_buttons"](c.get("msg_id"))
    if action == "yx":
        c["status"] = "skip"
        send(f"❌ Пропущено: {c['name']}", reply_to=c.get("msg_id"))
    elif action == "ys":
        c["status"] = "queued"
        send(f"📤 У черзі: {c['name']}. {queue_eta(s)}", reply_to=c.get("msg_id"))
    elif action == "yr":
        s["await"] = {"kind": "yt_rewrite", "pid": cid}
        send("Що змінити в листі? (напр. «коротше», «згадай інше відео»)\n"
             "Або свій текст, почавши з «текст:». Скасувати: «стоп»", reply_to=c.get("msg_id"))


def handle_rewrite(s, cid, t):
    c = _state(s)["ch"].get(cid)
    if not c:
        return
    if t.lower().startswith("текст:"):
        c["body"] = t[6:].strip()
    else:
        new = _f["ai_text"](f"Rewrite this email following the instruction. Keep it plain text, sign as Oleg, "
                            f"keep the last 'reply no' line.\nInstruction: {t}\nEmail:\n{c['body']}\n"
                            f"Return ONLY the new email text.")
        if not new:
            send("ШІ зараз недоступний. Надішли свій текст, почавши з «текст:»")
            s["await"] = {"kind": "yt_rewrite", "pid": cid}
            return
        c["body"] = new.strip().strip('"')
    show_draft(cid, c)


def queue_eta(s):
    y = _state(s)
    n = sum(1 for c in y["ch"].values() if c.get("status") == "queued")
    if y["paused"]:
        return "Розсилка на паузі (/go — продовжити)."
    return f"У черзі {n}; ліміт {DAILY_CAP}/день, будні 15:00–23:00 за Києвом."


def handle_command(s, t):
    """Returns True if the text was an outreach command."""
    y = _state(s)
    if t == "/pause":
        y["paused"] = True
        send("⏸ Розсилку листів зупинено. /go — продовжити.")
    elif t == "/go":
        y["paused"] = False
        y["bounces"] = []
        send("▶️ Розсилку продовжено. " + queue_eta(s))
    elif t == "/outreach":
        send(outreach_status(s))
    else:
        return False
    return True


def outreach_status(s):
    y = _state(s)
    by = {}
    for c in y["ch"].values():
        by[c.get("status")] = by.get(c.get("status"), 0) + 1
    now = time.time()
    return (f"📬 Листи каналам: чернеток {by.get('draft', 0)}, у черзі {by.get('queued', 0)}, "
            f"надіслано {by.get('sent', 0) + by.get('followed', 0)}, відповіли {by.get('replied', 0)}, "
            f"не дійшло {by.get('bounced', 0)}.\nЗа 24 год надіслано {len(_24h(y['sent_log'], now))}/{DAILY_CAP}."
            + (" ⏸ ПАУЗА" if y["paused"] else "") + f"\nРежим: {'авто' if AUTO else 'після твого 📤'}")

# ================================================================= sending

def can_send_now(y, now=None, utc=None):
    now = now or time.time()
    utc = utc or datetime.now(timezone.utc)
    if y["paused"] or utc.weekday() >= 5 or not SEND_HOURS_UTC[0] <= utc.hour < SEND_HOURS_UTC[1]:
        return False
    if len(_24h(y["sent_log"], now)) >= DAILY_CAP:
        return False
    return now - y["last_sent"] >= y["next_gap"]


def smtp_send(to, subject, body, reply_to_id=None):
    m = EmailMessage()
    m["From"] = email.utils.formataddr(("Oleg", GMAIL_USER))
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = email.utils.make_msgid(domain=GMAIL_USER.split("@")[-1])
    if reply_to_id:
        m["In-Reply-To"] = m["References"] = reply_to_id
    m.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as srv:
        srv.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        srv.send_message(m)
    return m["Message-ID"]


def pick_next(y, now):
    for cid, c in y["ch"].items():
        if c.get("status") == "queued":
            return cid, c, False
    for cid, c in y["ch"].items():
        if c.get("status") == "sent" and now - c.get("sent_at", now) > FOLLOWUP_AFTER_D * 86400:
            return cid, c, True
    return None, None, False


def send_due(s):
    y = _state(s)
    now = time.time()
    y["sent_log"] = _24h(y["sent_log"], now)
    if not can_send_now(y, now):
        return
    cid, c, follow = pick_next(y, now)
    if not c:
        return
    try:
        if follow:
            text = _f["ai_text"](FOLLOWUP_PROMPT.format(body=c["body"]))
            if not text:
                return
            smtp_send(c["email"], "Re: " + c["subject"], text.strip(), c.get("msgid"))
            c["status"] = "followed"
            send(f"🔁 Нагадування: {c['name']} ({c['email']})")
        else:
            c["msgid"] = smtp_send(c["email"], c["subject"], c["body"])
            c["status"], c["sent_at"] = "sent", now
            send(f"📤 Надіслано: {c['name']} · {c['subs']:,} підп.\n📧 {c['email']} · «{c['subject']}»")
    except smtplib.SMTPAuthenticationError:
        y["paused"] = True
        send("⚠️ Gmail не приймає вхід. Перевір GMAIL_USER і пароль застосунку (GMAIL_APP_PASSWORD). "
             "Розсилку зупинено, /go — продовжити.")
        return
    except Exception as e:
        log("smtp error", e)
        y["last_sent"], y["next_gap"] = now, 10 * 60
        return
    y["sent_log"].append(now)
    y["last_sent"] = now
    y["next_gap"] = (MIN_GAP_MIN + random.randint(0, 20)) * 60

# ================================================================= replies (Gmail IMAP)

def _hdr(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def check_replies(s):
    y = _state(s)
    contacted = {c["email"]: cid for cid, c in y["ch"].items()
                 if c.get("status") in ("sent", "followed") and c.get("email")}
    if not contacted:
        return
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as im:
        im.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        im.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%d-%b-%Y")
        _, data = im.uid("search", None, f"(SINCE {since})")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > y["imap_uid"]]
        for uid in uids:
            _, msg = im.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)] BODY.PEEK[TEXT]<0.3000>)")
            parts = [p[1] for p in msg if isinstance(p, tuple)]
            head = parts[0].decode(errors="replace") if parts else ""
            body = parts[1].decode(errors="replace") if len(parts) > 1 else ""
            fm = re.search(r"^From:(.*)$", head, re.I | re.M)
            frm = email.utils.parseaddr(_hdr(fm.group(1) if fm else ""))[1].lower()
            on_mail(s, frm, body, contacted)
            y["imap_uid"] = max(y["imap_uid"], uid)


def on_mail(s, frm, body, contacted):
    y = _state(s)
    if re.search(r"mailer-daemon|postmaster", frm):
        for addr, cid in contacted.items():
            if addr in body.lower():
                c = y["ch"][cid]
                c["status"] = "bounced"
                y["bounces"] = _24h(y["bounces"], time.time()) + [time.time()]
                send(f"✖️ Лист не дійшов: {c['name']} ({addr})")
                if len(y["bounces"]) >= BOUNCE_PAUSE:
                    y["paused"] = True
                    send(f"⏸ {BOUNCE_PAUSE} листи не дійшли за добу — розсилку зупинено, щоб Gmail не "
                         f"позначив тебе спамером. Перевір і напиши /go.")
        return
    cid = contacted.get(frm)
    if not cid:
        return
    c = y["ch"][cid]
    c["status"] = "replied"
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    text = re.split(r"On .{5,80} wrote:", text)[0].strip()[:700]
    send(f"📩🎬 ВІДПОВІДЬ від {c['name']} ({frm})\n\n{text}\n\n"
         f"🔗 Gmail: https://mail.google.com/mail/u/0/#search/from%3A{urllib.parse.quote(frm)}\n"
         f"Канал: {c['link']}")

# ================================================================= Reddit

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "linux:shorts-editor-watch:1.0 (by /u/oleg)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


ATOM = "{http://www.w3.org/2005/Atom}"


def parse_atom(raw):
    out = []
    for e in ET.fromstring(raw).iter(ATOM + "entry"):
        link = e.find(ATOM + "link")
        author = e.find(f"{ATOM}author/{ATOM}name")
        content = e.find(ATOM + "content")
        out.append({"id": (e.findtext(ATOM + "id") or "").strip(),
                    "title": (e.findtext(ATOM + "title") or "").strip(),
                    "link": link.get("href") if link is not None else "",
                    "author": (author.text or "").replace("/u/", "").strip() if author is not None else "",
                    "text": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                                   _unescape(content.text or ""))).strip() if content is not None else ""})
    return out


def _unescape(t):
    return html.unescape(t)


def is_reddit_lead(post):
    t = f"{post['title']} {post['text'][:1500]}"
    return bool(RD_TOPIC.search(t) and RD_HIRING.search(t) and not RD_NOT.search(post["title"]))


def compose_link(user, subject, text):
    return "https://www.reddit.com/message/compose/?" + urllib.parse.urlencode(
        {"to": user, "subject": subject[:100], "message": text})


def scan_reddit(s):
    _state(s)
    rd = s["rd"]
    seen = set(rd["seen"])
    first = not rd["seen"]
    for sub in RD_SUBS:
        try:
            raw = _get(f"https://www.reddit.com/r/{sub}/new/.rss?limit=50")
        except Exception as e:
            try:
                raw = _get(f"https://old.reddit.com/r/{sub}/new/.rss?limit=50")
            except Exception:
                log("reddit error", sub, e)
                continue
        for p in reversed(parse_atom(raw)):
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            rd["seen"].append(p["id"])
            if first or not is_reddit_lead(p):
                continue   # first run only remembers old posts
            msg = _f["ai_text"](REDDIT_PROMPT.format(service=SERVICE, portfolio=PORTFOLIO_URL or "(none yet)",
                                                     title=p["title"], text=p["text"][:2000]))
            if not msg:
                msg = ("Hi! I edit Shorts/Reels/TikToks (hooks, animated captions, b-roll, sound). "
                       f"Happy to do a free test edit for you. Portfolio: {PORTFOLIO_URL}")
            msg = msg.strip().strip('"')
            dm = compose_link(p["author"], "Re: " + p["title"], msg) if p["author"] else ""
            send(f"🟠 Reddit r/{sub}: {p['title']}\nu/{p['author']}\n\n{p['text'][:400]}"
                 f"{'…' if len(p['text']) > 400 else ''}\n🔗 {p['link']}\n\n✍️ Повідомлення:\n{msg}",
                 [[{"text": "✉️ Відкрити готове повідомлення", "url": dm}]] if dm else None)
    rd["seen"] = rd["seen"][-2000:]


def check_reddit_inbox(s):
    if not REDDIT_INBOX_RSS:
        return
    _state(s)
    rd = s["rd"]
    seen = set(rd["inbox_seen"])
    for m in reversed(parse_atom(_get(REDDIT_INBOX_RSS))):
        if m["id"] in seen:
            continue
        rd["inbox_seen"].append(m["id"])
        if rd["inbox_init"]:
            send(f"📩🟠 Reddit: u/{m['author']} — {m['title']}\n\n{m['text'][:700]}\n"
                 f"🔗 https://www.reddit.com/message/inbox")
    rd["inbox_init"] = True
    rd["inbox_seen"] = rd["inbox_seen"][-500:]

# ================================================================= loop hooks

def startup_lines():
    lines = []
    if YT_ON:
        lines.append(f"• листи YouTube-каналам: ✅ ({'авто' if AUTO else 'після твого 📤'}, до {DAILY_CAP}/день)"
                     + ("" if PORTFOLIO_URL else " ⚠️ немає PORTFOLIO_URL"))
    else:
        lines.append("• листи YouTube-каналам: вимкнено (потрібні YT_API_KEY, GMAIL_USER, GMAIL_APP_PASSWORD)")
    lines.append(f"• Reddit: {'✅' if RD_ON else 'вимкнено'}"
                 + (", вхідні ✅" if REDDIT_INBOX_RSS else ", вхідні не стежу (немає REDDIT_INBOX_RSS)"))
    return lines


def tick_scan(s):
    """Every scan (10 min): YouTube search every 6th time (~1 h), Reddit every time."""
    y = _state(s)
    y["ticks"] = y.get("ticks", 0) + 1
    if YT_ON and y["ticks"] % 6 == 1:
        try:
            discover(s)
        except Exception as e:
            log("youtube discover error", e)
    if RD_ON:
        try:
            scan_reddit(s)
        except Exception as e:
            log("reddit scan error", e)


def tick_track(s):
    """Every track (5 min): send what's due, check replies."""
    if YT_ON:
        try:
            send_due(s)
        except Exception as e:
            log("send error", e)
        try:
            check_replies(s)
        except Exception as e:
            log("imap error", e)
    try:
        check_reddit_inbox(s)
    except Exception as e:
        log("reddit inbox error", e)
