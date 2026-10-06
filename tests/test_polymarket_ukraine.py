"""Бэктест №3 «Украина»: тема, срок жизни, Telegram только папка и только каналы, посты строго до t, деньги."""
from __future__ import annotations

import asyncio
import importlib
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from forecast_bot.polymarket import backtest as B, series as S, tg_news as T

UTC = timezone.utc


def _runner():
    return importlib.import_module("tools.polymarket_ukraine")  # через sys.modules — чтобы мутации его видели


@pytest.mark.parametrize("title,ok", [
    ("Will Russia target Kyiv on...?", True), ("Moscow air traffic suspended by...?", True),
    ("Will Russia capture Dorozhnie by...?", True), ("Russia x Ukraine any diplomatic meeting by...?", True),
    ("Russia Parliamentary Election Winner", False), ("United Russia seats in the next Russian legislative", False),
    ("US x Iran Effective Ceasefire by...? (2 week pause)", False), ("Who will attend the NATO Summit?", False),
])
def test_on_topic(title, ok):
    assert _runner().on_topic(title) is ok


def _raw(life_days, closed=None, prices='["0", "1"]'):
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(days=life_days)
    return {"id": "1", "question": "Will Russia target Kyiv by September 30?", "slug": "s", "description": "rule",
            "startDate": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "closedTime": (closed or end).strftime("%Y-%m-%d %H:%M:%S+00"), "volumeNum": "5000",
            "outcomes": '["Yes", "No"]', "outcomePrices": prices, "clobTokenIds": '["a", "b"]',
            "umaResolutionStatus": "resolved", "feeType": "geopolitics_fees"}


def test_market_life_up_to_60_days_and_geopolitics_fee_zero():
    R = _runner()
    ev = {"id": 7, "title": "Will Russia target Kyiv by...?"}
    m = R.market_from(_raw(45), ev)
    assert m and m.fee_rate == 0.0 and m.end_planned == m.start + timedelta(days=45) and m.cluster() == "ev7"
    assert R.market_from(_raw(61), ev) is None and R.market_from(_raw(2), ev) is None
    # по плану 70 дней, досрочно закрыт на 20-й: проверка по closedTime его пропустила бы
    assert R.market_from(_raw(70, closed=datetime(2026, 8, 21, tzinfo=UTC)), ev) is None


def test_early_close_market_has_no_points_after_close():
    """«К дате» сыграло досрочно: точки от плановой endDate, закрытые до t — не прогнозируются."""
    R = _runner()
    m = R.market_from(_raw(40, closed=datetime(2026, 8, 5, tzinfo=UTC), prices='["1", "0"]'),
                      {"id": 7, "title": "Will Russia target Kyiv by...?"})
    assert m is not None and S.points(m) == {}


# ─────────────────────────── Telegram ──────────────────────────────
def _post(h, text, ch=1, base=datetime(2026, 9, 10, 12, tzinfo=UTC)):
    return T.Post(ch, int(h * 10 + 1000), base + timedelta(hours=h), text)


def test_select_posts_strictly_before_t_and_window():
    t = datetime(2026, 9, 10, 12, tzinfo=UTC)
    posts = [_post(-80, "Київ: вибухи"), _post(-5, "Атака на Київ, ракети"), _post(-1, "Погода в Одесі"),
             _post(0, "Удар по Києву В МОМЕНТ T"), _post(3, "Масована атака на Київ ПІСЛЯ T")]
    got = T.select(posts, t, ["київ", "києв", "ракет"])
    assert [p.text for p in got] == ["Атака на Київ, ракети"]   # −80 ч вне окна 3 суток, 0 и +3 ч — не раньше t


def test_as_research_has_channel_numbers_not_names():
    txt = T.as_research([_post(-2, "x" * 500, ch=12345)], {12345: 3})
    assert "channel 3" in txt and "12345" not in txt and len(txt.splitlines()[1]) < 400


class _Peer:
    def __init__(self, pid):
        self.channel_id = pid


class _Ent:
    def __init__(self, pid, broadcast):
        self.id, self.broadcast = pid, broadcast


class _Msg:
    def __init__(self, mid, date, text):
        self.id, self.date, self.message = mid, date, text


class FakeClient:
    """Папка 55: канал 1 и группа 2. Вне папки — личный чат 9. Любое обращение к 9 — провал."""

    def __init__(self):
        self.touched = []
        base = datetime(2026, 9, 10, tzinfo=UTC)
        self.msgs = {1: [_Msg(3, base + timedelta(hours=30), "після until"), _Msg(2, base + timedelta(hours=5), "пост"),
                         _Msg(1, base - timedelta(days=5), "до since")],
                     2: [_Msg(1, base, "група")], 9: [_Msg(1, base, "особисте")]}

    async def __call__(self, req):
        f55 = types.SimpleNamespace(id=55, include_peers=[_Peer(1), _Peer(2)])
        f7 = types.SimpleNamespace(id=7, include_peers=[_Peer(9)])
        return types.SimpleNamespace(filters=[f7, f55])

    async def get_entity(self, peer):
        self.touched.append(peer.channel_id)
        return _Ent(peer.channel_id, broadcast=peer.channel_id == 1)

    async def iter_messages(self, ent, offset_date=None):
        self.touched.append(("read", ent.id))
        for m in self.msgs[ent.id]:
            yield m


def test_fetch_reads_only_folder_channels():
    pytest.importorskip("telethon")
    c = FakeClient()
    base = datetime(2026, 9, 10, tzinfo=UTC)
    posts = asyncio.run(T.fetch(c, 55, base - timedelta(days=1), base + timedelta(days=1)))
    assert [p.text for p in posts] == ["пост"]
    assert 9 not in c.touched and ("read", 2) not in c.touched           # не папка и не канал — не читаются
    with pytest.raises(T.TgSetupError):
        asyncio.run(T.fetch(c, 99, base, base))


def test_credentials_without_values_in_errors(tmp_path):
    with pytest.raises(T.TgSetupError) as e:
        T.credentials(tmp_path / "absent.env")
    p = tmp_path / "tg.env"
    p.write_text("TG_API_ID=123\nTG_API_HASH=secret-hash\n")
    assert T.credentials(p) == (123, "secret-hash") and "secret" not in str(e.value)


def test_tg_data_outside_repo(monkeypatch):
    from forecast_bot import paths

    monkeypatch.delenv("FORECAST_STATE_DIR")
    d = T.tg_dir().resolve()
    assert paths.main_checkout().resolve() not in d.parents and d.name == "tg"


# ─────────────────────────── прогноз и деньги ──────────────────────
def _fixture(tmp_path, monkeypatch):
    R = _runner()
    monkeypatch.setattr(B, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(B, "CUTOFF", datetime(2026, 7, 1, tzinfo=UTC))
    # резолв на 3 ч позже планового конца (как у живых рынков) — фактическое закрытие ≠ плановому
    m = R.market_from(_raw(20, closed=datetime(2026, 8, 21, 3, tzinfo=UTC)), {"id": 7, "title": "Will Russia target Kyiv by...?"})
    m.history = [(int((m.start + timedelta(hours=h)).timestamp()), 0.4) for h in range(0, 24 * 20, 2)]
    (tmp_path / "ua").mkdir()
    (tmp_path / "ua" / "markets.jsonl").write_text(m.to_json() + "\n")
    (tmp_path / "ua" / "keywords.json").write_text(json.dumps({"7": ["київ", "kyiv"]}))
    pts = R.all_points([m])
    t_info = pts[0][3]
    (tmp_path / "tg").mkdir()
    (tmp_path / "tg" / "posts.jsonl").write_text("".join(p.to_json() + "\n" for p in [
        T.Post(1, 1, t_info - timedelta(hours=3), "Ракетна атака на Київ уночі"),
        T.Post(1, 2, t_info + timedelta(minutes=30), "КИЇВ ПІСЛЯ ЦІНИ РИНКУ"),
    ]))
    (tmp_path / "tg" / "channels.json").write_text(json.dumps({"1": 1}))
    return R, m, pts


def test_forecast_modes_use_only_info_before_market_price(tmp_path, monkeypatch):
    from forecast_bot import ai_guard, guarded_llm
    from forecast_bot.polymarket import forecaster

    R, m, pts = _fixture(tmp_path, monkeypatch)
    monkeypatch.setitem(ai_guard.APP_LIMITS, "polymarket", dict(ai_guard.APP_LIMITS["polymarket"]))
    monkeypatch.setattr(guarded_llm, "APP", "polymarket")
    monkeypatch.setattr(ai_guard, "spent_by_user", lambda user, since: (0.01, 1))
    seen = []

    async def fake(mk, point, t, research):
        seen.append((mk.closed, point, t, research))
        return 0.6, ""

    monkeypatch.setattr(forecaster, "forecast", fake)
    asyncio.run(R.run_forecast(types.SimpleNamespace(modes="none,gdelt,tg", points="t48,t50", limit=0)))
    rows = [json.loads(l) for l in (tmp_path / "ua" / "results.jsonl").read_text().splitlines()]
    assert {r["mode"] for r in rows} == {"none", "tg"}                  # gdelt без кэша — пропуск, не «без новостей»
    tg = [r for _, _, _, r in seen if r and "Telegram" in r]
    assert any("Ракетна атака" in r for r in tg)
    assert not any("ПІСЛЯ ЦІНИ" in r for r in tg)                      # пост после цены рынка не виден
    assert all(closed == m.end_planned for closed, *_ in seen)          # фактическое закрытие в вопрос не идёт
    assert all(t == next(p[3] for p in pts if p[1] == pt) for _, pt, t, _ in seen)  # «сегодня» = момент цены


def test_stage3_budget_counts_only_pm3_and_stops(tmp_path, monkeypatch, capsys):
    from forecast_bot import ai_guard, guarded_llm
    from forecast_bot.polymarket import forecaster

    R, m, pts = _fixture(tmp_path, monkeypatch)
    conn = ai_guard._conn()
    for user, cost in (("polymarket:pm2:ev1-t48", 9.0), ("polymarket:pm3:1-t48-none", 15.01)):
        conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                     (R.STAGE3_START.timestamp() + 60, "openrouter", "m", user, 0, 0, cost))
    conn.commit(); conn.close()
    assert R.stage_spent() == pytest.approx(15.01)
    monkeypatch.setitem(ai_guard.APP_LIMITS, "polymarket", dict(ai_guard.APP_LIMITS["polymarket"]))
    monkeypatch.setattr(guarded_llm, "APP", "polymarket")
    called = []

    async def fake(*a):
        called.append(a)
        return 0.5, ""

    monkeypatch.setattr(forecaster, "forecast", fake)
    asyncio.run(R.run_forecast(types.SimpleNamespace(modes="none", points="t48", limit=0)))
    assert called == [] and "потолок этапа $15.0 исчерпан" in capsys.readouterr().out


def test_report_compares_modes_on_common_points():
    R = _runner()
    base = {"fee_rate": 0.0, "volume": 5000.0, "p_mkt": 0.5, "outcome": 1, "point": "t48"}
    rows = []
    for i in range(6):
        for mode, p in (("none", 0.4), ("tg", 0.7)):
            rows.append(dict(base, market=f"m{i}", cluster=f"ev{i % 3}", mode=mode, p_bot=p))
    rows.append(dict(base, market="only-none", cluster="ev9", mode="none", p_bot=0.1))
    out = R.report(rows, boot=50)
    assert "Общих точек (все режимы): 6, событий 3" in out and "| t48 | tg | 6 | 3 |" in out
