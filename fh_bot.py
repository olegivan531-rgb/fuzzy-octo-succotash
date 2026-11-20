"""Freelancehunt assistant bot.

- Finds new relevant projects, AI rates fit and writes a tailored proposal.
- Sends each to Telegram with buttons: Submit / Rewrite / Reject / For Claude.
- Submit -> you confirm price and days -> bid is placed via Freelancehunt API.
- Tracks your bids and Freelancehunt notifications; when an employer picks you
  or writes to you, sends a separate Telegram message.

Run:  python fh_bot.py                 (runs forever)
      python fh_bot.py --minutes 350   (stops after N minutes; for GitHub Actions)

Env: FH_TOKEN, TG_BOT_TOKEN, TG_CHAT_ID
     AI (optional, tried in order): GROQ_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY
"""
import html, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta


def env(name, default=""):
    """Env var with stray spaces/newlines (common when pasting secrets) removed."""
    return (os.environ.get(name) or default).strip()


FH_TOKEN = env("FH_TOKEN")
TG_BOT_TOKEN = env("TG_BOT_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")
STATE_FILE = env("STATE_FILE", "state.json")
AI_MODEL = env("AI_MODEL", "claude-haiku-5-5")
AI_TIMEOUT = 30       # seconds per AI request

SCAN_EVERY_MIN = 10   # how often to look for new projects
TRACK_EVERY_MIN = 5   # how often to check bids and notifications
MAX_AGE_HOURS = 6
MAX_BIDS = 15
PAGES = 3
MIN_FIT = 5
HOURS_PER_DAY = 3     # your free hours per day, for the days estimate
REPORT_EVERY_H = 3    # short Telegram summary of what was checked

# Keywords match from the START of a word ("бот" matches "боти", not "роботу").
CATEGORIES = {
    "bot": ["telegram", "телеграм", "бот", "bot", "чат-бот", "chatbot", "viber"],
    "sheets": ["excel", "ексель", "эксель", "google sheets", "гугл табл", "таблиц",
               "формул", "apps script", "макрос", "vba", "звіт", "отчет", "облік", "учет"],
    "ai": ["chatgpt", "gpt", "claude", "openai", "штучн", "искусствен", " ai ", " ші ",
           "нейромереж", "нейросет", "промпт", "prompt", "автоматизац", "n8n", "make.com",
           "zapier", "парсинг", "парсер", "scraping"],
    "mobile": ["мобільн", "мобильн", "ios", "android", "flutter", "react native",
               "kotlin", "swift", "додаток", "приложени", "застосун", "mobile app"],
    "design": ["ui/ux", "ux/ui", "ui ", "ux ", "figma", "фігма", "фигма", "дизайн сайт",
               "дизайн лендинг", "дизайн застосун", "дизайн додат", "дизайн приложен",
               "дизайн інтерфейс", "дизайн интерфейс", "логотип", "logo", "банер", "баннер",
               "лендинг", "landing", "прототип", "брендинг", "фірмов", "фирменн"],
}
EXCLUDE = [
    # not our services
    "1с", "1c", "битрикс", "bitrix", "unity", "unreal",
    "таргет", "meta ads", "facebook ads", "google ads", "налаштування реклам", "настройка реклам", "рекламн кампан", "smm", "seo", "просуван", "продвижен",
    "копірайт", "копирайт", "рерайт", "переклад", "перевод",
    "шрифт", "купівл", "покупк", "куплю", "продати", "продать",
    "водіїв", "водител", "рекрут", "вакансі", "вакансия", "найм", "пошук співробітн",
    "інтер'єр", "интерьер", "екстер'єр", "экстерьер", "архітект", "архитект", "креслен", "чертеж",
    "autocad", "revit", "3d візуал", "3d визуал", "м2", "м²", "таунхаус", "будинк", "дома ",
    "реєстрація акаунт", "регистрация аккаунт", "відгуки на", "отзывы на", "накрут",
]

PROFILE = """Олег і команда розробників в Україні. Робимо:
- Telegram/Viber-боти: заявки, запис, FAQ, сповіщення, інтеграції з таблицями й CRM
- Excel / Google Sheets: формули, зведення, звіти, Apps Script
- ШІ-автоматизації: ChatGPT/Claude під процеси, n8n/Make, парсинг
- Мобільні застосунки (iOS/Android, Flutter/React Native)
- Дизайн: UI/UX у Figma, лендинги, банери, логотипи
Досвід: власний SaaS-продукт з ШІ для логістики США.
Не беремо: 1С, Bitrix, ігри, накрутки, таргетовану рекламу і SMM, SEO, копірайтинг і переклади,\nпошук персоналу, купівлю/продаж готових речей (шрифти, акаунти), архітектуру й дизайн інтер'єрів."""

AI_PROMPT = """Ти допомагаєш фрилансеру відбирати замовлення і писати відгуки.

Профіль виконавця:
{profile}

Замовлення з Freelancehunt:
Назва: {name}
Бюджет: {budget}
Відгуків уже: {bids}
Навички: {skills}
Опис: {desc}

Поверни ТІЛЬКИ JSON без пояснень:
{{"fit": <0-10: чи може виконавець реально це зробити і чи варто братися з огляду на бюджет і обсяг>,
  "why": "<1 коротке речення чому>",
  "hours": <оцінка годин роботи, число>,
  "price_uah": <розумна ціна в гривнях з урахуванням бюджету замовника, число>,
  "proposal": "<відгук мовою замовлення (укр/рос/англ), 60-110 слів: привітання; покажи, що зрозумів саме ЦЮ задачу (1-2 конкретні деталі з опису); як саме зробиш (2-3 кроки з конкретними інструментами); 1-2 уточнювальні питання; без шаблонних фраз, без емодзі; не вигадуй портфоліо чи відгуки; не обіцяй строк і ціну>"}}"""

REWRITE_PROMPT = """Перепиши відгук фрилансера на замовлення за вказівкою.
Замовлення: {name}
Опис: {desc}
Поточний відгук: {proposal}
Вказівка: {instruction}
Правила: 60-120 слів, без емодзі, без вигаданого портфоліо. Поверни ТІЛЬКИ новий текст відгуку."""

DRAFTS = {
    "bot": "Добрий день! Зроблю Telegram-бота під ваше завдання: {name}. Уточніть, будь ласка: "
           "які дані бот збирає і куди їх передавати (таблиця, адмінка, CRM)? Після відповіді дам точний строк.",
    "sheets": "Добрий день! Зроблю таблицю під ваше завдання: {name}. Надішліть, будь ласка, приклад "
              "поточного файлу (можна з тестовими даними), і я одразу скажу точний строк.",
    "ai": "Добрий день! Налаштую автоматизацію під ваше завдання: {name}. Розкажіть, будь ласка, як це "
          "робиться зараз вручну, і я запропоную найпростіший робочий варіант.",
    "mobile": "Добрий день! Наша команда зробить застосунок під ваше завдання: {name}. Уточніть, будь ласка: "
              "потрібні обидві платформи чи одна, і чи є готовий дизайн?",
    "design": "Добрий день! Зроблю дизайн під ваше завдання: {name}. Надішліть, будь ласка, 2-3 приклади, "
              "які вам подобаються, і бренд-матеріали, якщо є.",
}

FALLBACK = ("Добрий день! Уважно прочитав завдання «{name}». Щоб дати точну оцінку строку й вартості, "
            "уточніть, будь ласка, деталі: що вже є зараз і який результат вважаєте готовим? "
            "Після відповіді одразу запропоную план роботи.")

# ---------------------------------------------------------------- helpers

def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def http_json(url, headers=None, data=None, method=None, timeout=60):
    # Cloudflare in front of some APIs (e.g. Groq) answers 403 to the default Python-urllib agent
    headers = {"User-Agent": "Mozilla/5.0 (compatible; fh-bot/1.0)", **(headers or {})}
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from None


def fh(path, body=None):
    headers = {"Authorization": f"Bearer {FH_TOKEN}", "Accept-Language": "uk"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    return http_json(f"https://api.freelancehunt.com/v2{path}", headers, data,
                     method="POST" if body is not None else "GET")


def tg(method, **params):
    data = json.dumps(params).encode()
    res = http_json(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/{method}",
                    {"Content-Type": "application/json"}, data, timeout=70)
    return res.get("result")


def send(text, buttons=None, reply_to=None):
    p = {"chat_id": TG_CHAT_ID, "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    if reply_to:
        p["reply_to_message_id"] = reply_to
    return tg("sendMessage", **p)


def edit_buttons(msg_id, buttons=None):
    try:
        tg("editMessageReplyMarkup", chat_id=TG_CHAT_ID, message_id=msg_id,
           reply_markup={"inline_keyboard": buttons or []})
    except Exception as e:
        log("edit error", e)


def strip_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def load_state():
    if os.path.exists(STATE_FILE):
        s = json.load(open(STATE_FILE))
    else:
        s = {}
        # carry over projects already sent by the old fh_watch.py
        if os.path.exists("seen.json"):
            s["seen"] = [str(x) for x in json.load(open("seen.json"))]
    for k, v in {"seen": [], "tg_offset": 0, "projects": {}, "await": None,
                 "bids": {}, "feed_seen": [], "feed_init": False,
                 "stats": {}, "last_report": time.time()}.items():
        s.setdefault(k, v)
    return s


def save_state(s):
    s["seen"] = s["seen"][-3000:]
    s["feed_seen"] = s["feed_seen"][-500:]
    # keep only recent/active projects
    if len(s["projects"]) > 300:
        for pid in list(s["projects"])[:-300]:
            if pid not in s["bids"]:
                s["projects"].pop(pid, None)
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

# ---------------------------------------------------------------- AI

def _openai_compat(url, key, model, prompt):
    body = json.dumps({"model": model, "max_tokens": 900, "temperature": 0.5,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    res = http_json(url, {"Authorization": f"Bearer {key}", "content-type": "application/json"}, body,
                    timeout=AI_TIMEOUT)
    return res["choices"][0]["message"]["content"]


def _anthropic(key, model, prompt):
    body = json.dumps({"model": model, "max_tokens": 900,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    res = http_json("https://api.anthropic.com/v1/messages", {
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, body,
                    timeout=AI_TIMEOUT)
    return "".join(b.get("text", "") for b in res.get("content", []))


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# Tried in order; the first one with a key that answers wins.
PROVIDERS = [
    ("groq", "GROQ_API_KEY", lambda k, p: _openai_compat(
        "https://api.groq.com/openai/v1/chat/completions",
        k, env("GROQ_MODEL") or "openai/gpt-oss-120b", p)),
    ("gemini", "GEMINI_API_KEY", lambda k, p: _openai_compat(
        GEMINI_URL, k, env("GEMINI_MODEL") or "gemini-flash-latest", p)),
    # lighter model: usually available when the main one answers 503 "high demand"
    ("gemini-lite", "GEMINI_API_KEY", lambda k, p: _openai_compat(
        GEMINI_URL, k, env("GEMINI_LITE_MODEL") or "gemini-flash-lite-latest", p)),
    ("anthropic", "ANTHROPIC_API_KEY", lambda k, p: _anthropic(k, AI_MODEL, p)),
]


def ai_text(prompt):
    for pname, env_name, call in PROVIDERS:
        key = env(env_name)
        if not key:
            continue
        try:
            return call(key, prompt)
        except Exception as e:
            log(f"ai error ({pname}):", e)
    return None


def ai_review(name, desc, skills, budget_s, bids):
    text = ai_text(AI_PROMPT.format(profile=PROFILE, name=name, budget=budget_s,
                                    bids=bids, skills=skills, desc=desc[:3000]))
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None

def startup_report():
    lines = ["🤖 Бот запущено. Перевірка ШІ:"]
    ok = False
    for pname, env_name, call in PROVIDERS:
        key = env(env_name)
        if not key:
            lines.append(f"• {pname}: ключ не задано")
            continue
        try:
            call(key, "Відповідай одним словом: ок")
            lines.append(f"• {pname}: ✅ працює")
            ok = True
        except Exception as e:
            lines.append(f"• {pname}: ❌ {str(e)[:150]}")
    if not ok:
        lines.append("\n⚠️ Жоден ШІ не працює — відгуки будуть заготовками. Перевір ключі в Secrets.")
    return "\n".join(lines)

# ---------------------------------------------------------------- projects

def _hits(t, kw):
    return len(re.findall(r"(?<![\w'’])" + re.escape(kw), t))


def classify(text):
    t = f" {text.lower()} "
    if any(_hits(t, x) for x in EXCLUDE):
        return None
    scores = {c: sum(_hits(t, k) for k in kws) for c, kws in CATEGORIES.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def project_buttons(pid):
    return [[{"text": "✅ Подати", "callback_data": f"ok:{pid}"},
             {"text": "✏️ Переписати", "callback_data": f"rw:{pid}"}],
            [{"text": "❌ Відхилити", "callback_data": f"no:{pid}"},
             {"text": "📋 Для Claude", "callback_data": f"cl:{pid}"}]]


def project_text(p):
    return (f"🆕 {p['name']}\n💰 {p['budget_s']} · відгуків: {p['bids']} · [{p['cat']}]\n{p['head']}\n\n"
            f"{p['desc'][:350]}{'…' if len(p['desc']) > 350 else ''}\n🔗 {p['link']}\n\n"
            f"✍️ Відгук:\n{p['proposal']}")


def scan_projects(s):
    seen = set(s["seen"])
    now = datetime.now(timezone.utc)
    found = []
    for page in range(1, PAGES + 1):
        res = fh(f"/projects?page[number]={page}")
        found.extend(res.get("data", []))
        if not res.get("links", {}).get("next"):
            break
    sent = 0
    st = s["stats"]
    def count(k):
        st[k] = st.get(k, 0) + 1
    for it in found:
        pid = str(it.get("id"))
        if pid in seen:
            continue
        seen.add(pid)
        s["seen"].append(pid)
        count("new")
        a = it.get("attributes", {})
        pub = parse_dt(a.get("published_at") or "")
        if pub and now - pub > timedelta(hours=MAX_AGE_HOURS):
            count("old")
            continue
        bids = a.get("bid_count") or 0
        if bids > MAX_BIDS:
            count("crowded")
            continue
        name = a.get("name", "")
        desc = re.sub(r"\s+", " ", a.get("description") or "")
        skills = ", ".join(x.get("name", "") for x in a.get("skills") or [])
        cat = classify(f"{name} {desc} {skills}")
        if not cat:
            count("off_topic")
            continue
        budget = a.get("budget") or {}
        budget_s = f"{budget.get('amount')} {budget.get('currency')}" if budget else "не вказано"
        link = it.get("links", {}).get("self") or {}
        link = link.get("web") if isinstance(link, dict) else link

        r = ai_review(name, desc, skills, budget_s, bids)
        hours, price = None, None
        if r is not None:
            try:
                fit = int(r.get("fit", 0))
            except (TypeError, ValueError):
                fit = 0
            if fit < MIN_FIT:
                count("low_fit")
                continue
            hours, price = r.get("hours"), r.get("price_uah")
            head = f"⭐ {fit}/10 · ~{hours or '?'} год · {r.get('why', '')}"
            proposal = r.get("proposal", "")
        else:
            head = "⚠️ ШІ недоступний — відгук-заготовка, перепиши перед подачею"
            proposal = FALLBACK.format(name=name)

        p = {"name": name, "desc": desc, "skills": skills, "cat": cat, "bids": bids,
             "budget_s": budget_s, "budget_amount": budget.get("amount"),
             "currency": budget.get("currency") or "UAH", "safe_type": a.get("safe_type"),
             "link": link, "head": head, "proposal": proposal, "hours": hours,
             "price": price, "status": "new"}
        msg = send(project_text(p), project_buttons(pid))
        p["msg_id"] = msg["message_id"] if msg else None
        s["projects"][pid] = p
        sent += 1
        count("sent")
    log(f"scan: {len(found)} checked, {sent} sent")


def suggest_terms(p):
    try:
        hours = float(p.get("hours") or 0)
    except (TypeError, ValueError):
        hours = 0
    days = max(1, math.ceil(hours / HOURS_PER_DAY)) if hours else 3
    price = p.get("price") or p.get("budget_amount") or 1000
    try:
        price = int(float(price))
    except (TypeError, ValueError):
        price = 1000
    return price, days


def place_bid(s, pid, price, days):
    p = s["projects"][pid]
    body = {"days": int(days), "budget": {"amount": int(price), "currency": p.get("currency") or "UAH"},
            "safe_type": p.get("safe_type"), "comment": p["proposal"], "is_hidden": False}
    try:
        res = fh(f"/projects/{pid}/bids", body)
    except Exception as e:
        send(f"⚠️ Не вдалося подати відгук на «{p['name']}»:\n{e}", reply_to=p.get("msg_id"))
        return
    bid_id = str((res.get("data") or {}).get("id", ""))
    p["status"] = "submitted"
    s["bids"][pid] = {"bid_id": bid_id, "status": "active", "winner": False, "name": p["name"],
                      "link": p["link"], "price": price, "days": days}
    edit_buttons(p.get("msg_id"))
    send(f"✅ Відгук подано: {price} {p.get('currency') or 'UAH'}, {days} дн.\n«{p['name']}»",
         reply_to=p.get("msg_id"))

# ---------------------------------------------------------------- telegram updates

def handle_callback(s, cq):
    data = cq.get("data", "")
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    if ":" not in data:
        return
    action, pid = data.split(":", 1)
    p = s["projects"].get(pid)
    if not p:
        send("Це замовлення вже не відстежується.")
        return
    if action == "no":
        p["status"] = "rejected"
        edit_buttons(p.get("msg_id"))
        send(f"❌ Відхилено: «{p['name']}»", reply_to=p.get("msg_id"))
    elif action == "ok":
        price, days = suggest_terms(p)
        s["await"] = {"kind": "terms", "pid": pid, "price": price, "days": days}
        send(f"Подати відгук на «{p['name']}»?\nПропоную: {price} {p.get('currency') or 'UAH'}, {days} дн.\n\n"
             f"Напиши «ок», або свої ціну і дні, напр.: 3000 4\nСкасувати: «стоп»",
             reply_to=p.get("msg_id"))
    elif action == "rw":
        s["await"] = {"kind": "rewrite", "pid": pid}
        send("Що змінити у відгуку? Напиши вказівку (напр.: «коротше», «додай про досвід з n8n»)\n"
             "або надішли повний новий текст, почавши з «текст:»\nСкасувати: «стоп»",
             reply_to=p.get("msg_id"))
    elif action == "cl":
        send("Скопіюй і встав у чат із Claude:\n\n"
             f"Допоможи з замовленням на Freelancehunt.\nНазва: {p['name']}\nБюджет: {p['budget_s']}\n"
             f"Посилання: {p['link']}\nОпис: {p['desc'][:2500]}\n\nМій чорновик відгуку:\n{p['proposal']}",
             reply_to=p.get("msg_id"))


def handle_text(s, text):
    w = s.get("await")
    t = text.strip()
    if not w:
        send("Команди: кнопки під замовленнями. /status — що зараз відстежується.") \
            if t != "/status" else send(status_text(s))
        return
    if t.lower() in ("стоп", "stop", "скасувати"):
        s["await"] = None
        send("Скасовано.")
        return
    pid = w["pid"]
    p = s["projects"].get(pid)
    if not p:
        s["await"] = None
        return
    if w["kind"] == "terms":
        if t.lower() in ("ок", "ok", "так", "+"):
            price, days = w["price"], w["days"]
        else:
            nums = re.findall(r"\d+", t)
            if len(nums) < 2:
                send("Не зрозумів. Напиши «ок» або ціну і дні, напр.: 3000 4")
                return
            price, days = int(nums[0]), int(nums[1])
        s["await"] = None
        place_bid(s, pid, price, days)
    elif w["kind"] == "rewrite":
        s["await"] = None
        if t.lower().startswith("текст:"):
            p["proposal"] = t[6:].strip()
        else:
            new = ai_text(REWRITE_PROMPT.format(name=p["name"], desc=p["desc"][:2500],
                                                proposal=p["proposal"], instruction=t))
            if not new:
                send("ШІ зараз недоступний. Надішли свій текст, почавши з «текст:»")
                s["await"] = {"kind": "rewrite", "pid": pid}
                return
            p["proposal"] = new.strip().strip('"')
        edit_buttons(p.get("msg_id"))
        msg = send(project_text(p), project_buttons(pid))
        p["msg_id"] = msg["message_id"] if msg else p.get("msg_id")


def status_text(s):
    lines = ["📊 Подані відгуки:"]
    for pid, b in s["bids"].items():
        mark = "🏆" if b.get("winner") else {"active": "⏳", "rejected": "✖️", "revoked": "↩️"}.get(b["status"], "•")
        lines.append(f"{mark} {b['name']} — {b['price']} грн, {b['days']} дн.")
    if len(lines) == 1:
        lines.append("поки немає")
    return "\n".join(lines)


def poll_telegram(s, wait):
    try:
        updates = tg("getUpdates", offset=s["tg_offset"], timeout=wait,
                     allowed_updates=["message", "callback_query"]) or []
    except Exception as e:
        log("tg poll error", e)
        time.sleep(5)
        return
    for u in updates:
        s["tg_offset"] = u["update_id"] + 1
        try:
            if "callback_query" in u:
                cq = u["callback_query"]
                if str(cq.get("message", {}).get("chat", {}).get("id")) == TG_CHAT_ID:
                    handle_callback(s, cq)
            elif "message" in u:
                m = u["message"]
                if str(m.get("chat", {}).get("id")) == TG_CHAT_ID and m.get("text"):
                    handle_text(s, m["text"])
        except Exception as e:
            log("update error", e)
            send(f"⚠️ Помилка: {e}")
        save_state(s)

# ---------------------------------------------------------------- tracking

# whole-word-start matches only: "обра" must not fire on "зображень"
IMPORTANT_RE = re.compile(
    r"(?<!\w)(обрал|обрано|обрав|обрала|выбрал|выбран|переможц|победител"
    r"|вас.{0,20}виконавц|вас.{0,20}исполнител|умови співпрац|условия сотрудничеств)",
    re.I)


def track(s):
    # 1) Freelancehunt notifications (employer chose you, messages, etc.)
    try:
        feed = fh("/my/feed").get("data", [])
        seen = set(s["feed_seen"])
        for it in reversed(feed):
            fid = str(it.get("id"))
            if fid in seen:
                continue
            s["feed_seen"].append(fid)
            if not s["feed_init"]:
                continue  # first run: don't flood with old notifications
            a = it.get("attributes", {})
            sender = (a.get("from") or {}).get("login") if isinstance(a.get("from"), dict) else None
            raw = a.get("message") or ""
            msg = strip_html(raw)
            important = IMPORTANT_RE.search(msg)
            href = re.search(r'href=["\']([^"\']+)', raw)
            link = href.group(1) if href else ""
            if link.startswith("/"):
                link = "https://freelancehunt.com" + link
            send(("🏆🔔 " if important else "🔔 ") + (f"{sender}: " if sender else "") + msg
                 + (f"\n🔗 {link}" if link else ""))
        s["feed_init"] = True
    except Exception as e:
        log("feed error", e)

    # 2) Status of your bids
    active = {pid: b for pid, b in s["bids"].items() if b["status"] == "active" and not b.get("winner")}
    if not active:
        return
    try:
        mine = {}
        for it in fh("/my/bids").get("data", []):
            mine[str(it.get("id"))] = it.get("attributes", {})
    except Exception as e:
        log("bids error", e)
        mine = {}
    for pid, b in active.items():
        a = mine.get(b.get("bid_id"), {})
        st = a.get("status")
        if st and st != b["status"]:
            b["status"] = st
            if st == "rejected":
                send(f"✖️ Замовник відхилив відгук: «{b['name']}»\n🔗 {b['link']}")
        try:
            w = fh(f"/projects/{pid}/bids?filter[is_winner]=1").get("data", [])
            if any(str(x.get("id")) == b.get("bid_id") for x in w):
                b["winner"] = True
                send(f"🏆 ВАС ОБРАНО ВИКОНАВЦЕМ!\n«{b['name']}»\n💰 {b['price']} грн · {b['days']} дн.\n"
                     f"🔗 {b['link']}\n\nНаступний крок: напиши замовнику й уточни деталі.")
            elif w:
                b["status"] = "lost"
                send(f"ℹ️ Замовник обрав іншого виконавця: «{b['name']}»")
        except Exception as e:
            log("winner check error", e)

def report(s):
    st = s["stats"]
    hours = max(1, round((time.time() - s["last_report"]) / 3600))
    send(f"📈 За ~{hours} год: нових замовлень {st.get('new', 0)}, надіслано {st.get('sent', 0)}.\n"
         f"Пропущено: не наш профіль {st.get('off_topic', 0)}, оцінка ШІ < {MIN_FIT} — "
         f"{st.get('low_fit', 0)}, понад {MAX_BIDS} відгуків {st.get('crowded', 0)}, "
         f"старіші за {MAX_AGE_HOURS} год {st.get('old', 0)}.")
    s["stats"] = {}
    s["last_report"] = time.time()

# ---------------------------------------------------------------- main loop

def main():
    missing = [n for n in ("FH_TOKEN", "TG_BOT_TOKEN", "TG_CHAT_ID") if not env(n)]
    if missing:
        sys.exit("missing secrets: " + ", ".join(missing))
    minutes = None
    if "--minutes" in sys.argv:
        minutes = float(sys.argv[sys.argv.index("--minutes") + 1])
    end = time.time() + minutes * 60 if minutes else None
    s = load_state()
    last_scan = last_track = 0
    log("bot started")
    send(startup_report())
    while end is None or time.time() < end:
        now = time.time()
        if now - last_scan >= SCAN_EVERY_MIN * 60:
            try:
                scan_projects(s)
            except Exception as e:
                log("scan error", e)
            last_scan = now
            save_state(s)
        if now - s["last_report"] >= REPORT_EVERY_H * 3600:
            try:
                report(s)
            except Exception as e:
                log("report error", e)
            save_state(s)
        if now - last_track >= TRACK_EVERY_MIN * 60:
            track(s)
            last_track = now
            save_state(s)
        wait = 50 if end is None else max(1, min(50, int(end - time.time())))
        poll_telegram(s, wait)
    save_state(s)
    log("bot stopped")


if __name__ == "__main__":
    main()
