"""Freelancehunt watcher: finds new relevant projects, has AI judge the fit
and write a proposal tailored to the task, sends it all to your Telegram.
You send the proposal, negotiate terms and do the work yourself.

Env vars: FH_TOKEN, TG_BOT_TOKEN, TG_CHAT_ID; AI (optional, any): GEMINI_API_KEY, GROQ_API_KEY, ANTHROPIC_API_KEY
"""
import json, os, re, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

def env(name, default=""):
    """Env var with stray spaces/newlines (common when pasting secrets) removed."""
    return (os.environ.get(name) or default).strip()


FH_TOKEN = env("FH_TOKEN")
TG_BOT_TOKEN = env("TG_BOT_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")
AI_MODEL = env("AI_MODEL", "claude-haiku-5-5")
AI_TIMEOUT = 30     # seconds per AI request
SEEN_FILE = env("SEEN_FILE", "seen.json")

MAX_AGE_HOURS = 6   # ignore older projects
MAX_BIDS = 15       # skip crowded projects
PAGES = 3           # newest project pages to scan
MIN_FIT = 6         # 0-10: send only projects the AI rates at least this

# --- pre-filter by keywords (lowercase, ua/ru/en); AI decides the final fit
CATEGORIES = {
    "bot": ["telegram", "телеграм", "бот", "bot", "чат-бот", "chatbot", "viber"],
    "sheets": ["excel", "ексель", "эксель", "google sheets", "гугл табл", "таблиц",
               "формул", "apps script", "макрос", "vba", "звіт", "отчет", "облік", "учет"],
    "ai": ["chatgpt", "gpt", "claude", "openai", "штучн", "искусствен", " ai ", " ші ",
           "нейромереж", "нейросет", "промпт", "prompt", "автоматизац", "n8n", "make.com",
           "zapier", "парсинг", "парсер", "scraping"],
    "mobile": ["мобільн", "мобильн", "ios", "android", "flutter", "react native",
               "kotlin", "swift", "додаток", "приложени", "застосун", "mobile app"],
    "design": ["дизайн", "design", "figma", "фігма", "ui/ux", "ux/ui", " ui ", " ux ",
               "логотип", "logo", "банер", "баннер", "лендинг", "landing", "прототип",
               "макет", "брендинг", "креатив"],
}
EXCLUDE = ["1с", "1c:", " 1c ", "битрикс", "bitrix", "unity", "unreal",
           "реєстрація акаунт", "регистрация аккаунт", "відгуки на", "накрут"]

PROFILE = """Олег і команда розробників в Україні. Робимо:
- Telegram/Viber-боти: заявки, запис, FAQ, сповіщення, інтеграції з таблицями й CRM
- Excel / Google Sheets: формули, зведення, звіти, Apps Script
- ШІ-автоматизації: ChatGPT/Claude під процеси, n8n/Make, парсинг
- Мобільні застосунки (iOS/Android, Flutter/React Native)
- Дизайн: UI/UX у Figma, лендинги, банери, логотипи
Досвід: власний SaaS-продукт з ШІ для логістики США.
Не беремо: 1С, Bitrix, ігри, накрутки."""

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
  "proposal": "<відгук мовою замовлення (укр/рос/англ), 60-110 слів: привітання; покажи, що зрозумів саме ЦЮ задачу (1-2 конкретні деталі з опису); як саме зробиш (2-3 кроки з конкретними інструментами); 1-2 уточнювальні питання; без шаблонних фраз, без емодзі; не вигадуй портфоліо чи відгуки; не обіцяй строк і ціну>"}}"""

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


def http_json(url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_projects():
    out = []
    for page in range(1, PAGES + 1):
        res = http_json(f"https://api.freelancehunt.com/v2/projects?page[number]={page}",
                        {"Authorization": f"Bearer {FH_TOKEN}", "Accept-Language": "uk"})
        out.extend(res.get("data", []))
        if not res.get("links", {}).get("next"):
            break
    return out


def classify(text):
    t = f" {text.lower()} "
    if any(x in t for x in EXCLUDE):
        return None
    scores = {c: sum(t.count(k) for k in kws) for c, kws in CATEGORIES.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def _openai_compat(url, key, model, prompt):
    body = json.dumps({"model": model, "max_tokens": 800, "temperature": 0.5,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    res = http_json(url, {"Authorization": f"Bearer {key}",
                          "content-type": "application/json"}, data=body, timeout=AI_TIMEOUT)
    return res["choices"][0]["message"]["content"]


def _anthropic(key, model, prompt):
    body = json.dumps({"model": model, "max_tokens": 800,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    res = http_json("https://api.anthropic.com/v1/messages", {
        "x-api-key": key, "anthropic-version": "2023-06-01",
        "content-type": "application/json"}, data=body, timeout=AI_TIMEOUT)
    return "".join(b.get("text", "") for b in res.get("content", []))


# Providers tried in order; the first one with a key that answers wins.
PROVIDERS = [
    ("groq", "GROQ_API_KEY", lambda k, p: _openai_compat(
        "https://api.groq.com/openai/v1/chat/completions",
        k, env("GROQ_MODEL") or "openai/gpt-oss-120b", p)),
    ("gemini", "GEMINI_API_KEY", lambda k, p: _openai_compat(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        k, env("GEMINI_MODEL") or "gemini-flash-latest", p)),
    ("anthropic", "ANTHROPIC_API_KEY", lambda k, p: _anthropic(k, AI_MODEL, p)),
]


def ai_review(name, desc, skills, budget_s, bids):
    """dict(fit, why, hours, proposal) or None if no AI key works."""
    prompt = AI_PROMPT.format(profile=PROFILE, name=name, budget=budget_s, bids=bids,
                              skills=skills, desc=desc[:3000])
    for pname, env_name, call in PROVIDERS:
        key = env(env_name)
        if not key:
            continue
        try:
            text = call(key, prompt)
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                return json.loads(m.group(0))
        except Exception as e:
            print(f"ai error ({pname}):", e, file=sys.stderr)
    return None


def parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def tg_send(text):
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    http_json(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", data=data)


def main():
    seen = set(json.load(open(SEEN_FILE))) if os.path.exists(SEEN_FILE) else set()
    now = datetime.now(timezone.utc)
    sent = 0
    for p in fetch_projects():
        pid = str(p.get("id"))
        a = p.get("attributes", {})
        if pid in seen:
            continue
        seen.add(pid)
        pub = parse_dt(a.get("published_at") or "")
        if pub and now - pub > timedelta(hours=MAX_AGE_HOURS):
            continue
        bids = a.get("bid_count") or 0
        if bids > MAX_BIDS:
            continue
        name = a.get("name", "")
        desc = re.sub(r"\s+", " ", a.get("description") or "")
        skills = ", ".join(s.get("name", "") for s in a.get("skills") or [])
        cat = classify(f"{name} {desc} {skills}")
        if not cat:
            continue
        budget = a.get("budget") or {}
        budget_s = f"{budget.get('amount')} {budget.get('currency')}" if budget else "не вказано"
        link = p.get("links", {}).get("self") or {}
        link = link.get("web") if isinstance(link, dict) else link

        r = ai_review(name, desc, skills, budget_s, bids)
        if r is not None:
            try:
                fit = int(r.get("fit", 0))
            except (TypeError, ValueError):
                fit = 0
            if fit < MIN_FIT:
                continue
            head = f"⭐ {fit}/10 · ~{r.get('hours', '?')} год · {r.get('why', '')}"
            proposal = r.get("proposal", "")
        else:
            head = "(ШІ вимкнено — шаблон)"
            proposal = DRAFTS[cat].format(name=name)

        tg_send(f"🆕 {name}\n💰 {budget_s} · відгуків: {bids} · [{cat}]\n{head}\n\n"
                f"{desc[:350]}{'…' if len(desc) > 350 else ''}\n🔗 {link}\n\n"
                f"✍️ Відгук:\n{proposal}")
        sent += 1
    json.dump(sorted(seen, key=lambda x: int(x) if x.isdigit() else 0)[-3000:],
              open(SEEN_FILE, "w"))
    print(f"sent {sent}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("error:", e, file=sys.stderr)
        sys.exit(1)
