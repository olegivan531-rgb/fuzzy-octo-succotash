"""Price / currency / days rules of fh_bot.suggest_terms. No network, no secrets."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for k in ("FH_TOKEN", "TG_BOT_TOKEN", "TG_CHAT_ID"):
    os.environ.pop(k, None)          # importing the bot must not need secrets

import fh_bot as b  # noqa: E402


def proj(**kw):
    p = {"name": "Проєкт", "desc": "", "cat": "ai", "currency": "UAH"}
    p.update(kw)
    return p


def test_import_needs_no_secrets():
    assert b.FH_TOKEN == "" and b.TG_BOT_TOKEN == ""


def test_book_editing_288_pages():
    p = proj(budget_amount=25000, hours=15, cat="translate",
             name="Редагування книжки", desc="Потрібне редагування рукопису, 288 сторінок")
    price, days, warnings = b.suggest_terms(p)
    assert days >= 90
    assert 17500 <= price <= 25000


def test_eur_budget_ai_price_in_uah_is_not_shown_as_eur():
    price, _, _ = b.suggest_terms(proj(currency="EUR", budget_amount=100, price_uah=30))
    assert 70 <= price <= 100


def test_eur_low_ai_price_raised_to_floor():
    # 3000 UAH ~ 62.5 EUR -> below 70% of 100 EUR
    price, _, _ = b.suggest_terms(proj(currency="EUR", budget_amount=100, price_uah=3000))
    assert price >= 70


def test_uah_low_ai_price_raised_to_floor():
    price, _, _ = b.suggest_terms(proj(budget_amount=2500, price_uah=1000, hours=3))
    assert price >= 1750


def test_small_budget_warns():
    price, _, warnings = b.suggest_terms(proj(budget_amount=500, hours=20))
    assert price == 500
    assert any("бюджет замалий" in w for w in warnings)


def test_pln_without_budget():
    price, days, _ = b.suggest_terms(proj(currency="PLN", hours=10))
    assert price == b.from_uah(4000, "PLN")
    assert price % 50 == 0
    assert days == 4


def test_no_budget_no_hours():
    price, days, warnings = b.suggest_terms(proj())
    assert b.to_uah(price, "UAH") >= b.MIN_PRICE_UAH
    assert days == 3
    assert any("без оцінки" in w for w in warnings)


def test_unknown_currency_does_not_crash():
    price, days, _ = b.suggest_terms(proj(currency="XYZ", budget_amount=100, hours=5))
    assert price > 0 and days >= 1


def test_old_state_entries_do_not_crash():
    old_fh = {"name": "x", "desc": "", "cat": "bot", "currency": "UAH", "price": 1200,
              "budget_amount": None, "hours": None}
    minimal = {"name": "z"}
    for p in (old_fh, minimal):
        price, days, warnings = b.suggest_terms(p)
        assert price > 0 and days >= 1
    assert b.suggest_terms(old_fh)[0] == 1200    # UAH price from the AI is still honoured


def test_round_trip_eur():
    assert abs(b.from_uah(b.to_uah(100, "EUR"), "EUR") - 100) <= 5


def test_min_price_only_without_budget():
    # budget 300 UAH: never go above the budget just to reach MIN_PRICE_UAH
    price, _, _ = b.suggest_terms(proj(budget_amount=300, hours=1))
    assert price <= 300


@pytest.mark.parametrize("text,expected", [
    ("Дизайн 5 екранів застосунку", 20),
    ("Заповнити 400 товарів у каталозі", 20),
    ("Імпорт 2000 позицій", 40),          # capped
    ("Бюджет 15000 грн, 2025 рік", 0),     # prices / years are not volume
])
def test_volume_hours(text, expected):
    assert b.volume_hours({"name": "", "desc": text, "cat": "web"}) == expected


def test_project_text_shows_currency_and_uah():
    p = proj(currency="EUR", budget_amount=100, price_uah=3000, hours=6, budget_s="100 EUR", bids=2,
             head="⭐ 8/10", link="https://example.test/p/1", proposal="Текст")
    text = b.project_text(p)
    assert "💵 Ставка: 70 EUR (~3360 грн) · строк: 2 дн. · ~6 год" in text


def test_project_text_uah_has_no_duplicate():
    p = proj(budget_amount=2500, price_uah=2000, hours=3, budget_s="2500 UAH", bids=1,
             head="⭐ 8/10", link="https://example.test/p/2", proposal="Текст")
    line = next(l for l in b.project_text(p).splitlines() if l.startswith("💵"))
    assert "(~" not in line and "UAH" in line


def test_status_and_winner_use_bid_currency():
    s = {"bids": {"1": {"name": "A", "price": 70, "days": 2, "status": "manual", "currency": "EUR"},
                  "2": {"name": "B", "price": 1500, "days": 1, "status": "manual"}},
         "projects": {"2": {"currency": "UAH"}}}
    text = b.status_text(s)
    assert "70 EUR" in text and "1500 грн" in text


def test_timeouts_skip_provider(monkeypatch):
    calls = []

    def slow(k, p):
        calls.append("slow")
        raise TimeoutError("The read operation timed out")

    def ok(k, p):
        calls.append("ok")
        return "fine"

    monkeypatch.setattr(b, "PROVIDERS", [("slow", "K1", slow), ("ok", "K2", ok)])
    monkeypatch.setattr(b, "AI_TIMEOUTS", {})
    monkeypatch.setenv("K1", "x")
    monkeypatch.setenv("K2", "y")
    for _ in range(4):
        assert b.ai_text("hi") == "fine"
    assert calls.count("slow") == 2      # skipped after 2 timeouts in a row


def test_freelancer_entries_are_dropped_from_old_state(tmp_path, monkeypatch):
    import json
    f = tmp_path / "state.json"
    f.write_text(json.dumps({
        "projects": {"123": {"name": "fh", "currency": "UAH"},
                     "fl999": {"name": "fl", "site": "freelancer", "currency": "USD"}},
        "bids": {"fl999": {"name": "fl", "price": 200, "days": 2, "status": "manual"}},
        "await": {"kind": "terms", "pid": "fl999", "price": 200, "days": 2}}))
    monkeypatch.setattr(b, "STATE_FILE", str(f))
    s = b.load_state()
    assert list(s["projects"]) == ["123"] and s["bids"] == {} and s["await"] is None


def test_no_freelancer_code_left():
    src = open(b.__file__, encoding="utf-8").read()
    assert "freelancer.com/api" not in src and "scan_freelancer" not in src and "FL_" not in src
