"""Этап 4 Polymarket (без прогноза): перекосы цены, несогласованность, скорость, маркетмейкинг."""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from forecast_bot.polymarket import backtest as B, biases as X, series as S

UTC = timezone.utc
H = 3600


def _runner():
    return importlib.import_module("tools.polymarket_biases")


# ─────────────────────────── А ─────────────────────────────────────
def test_bucket_and_period():
    assert X.bucket(0.01) == "long" and X.bucket(0.0999) == "long" and X.bucket(0.10) is None
    assert X.bucket(0.90) is None and X.bucket(0.95) == "fav" and X.bucket(0.99) == "fav" and X.bucket(0.995) is None
    assert X.period(datetime(2026, 7, 5, tzinfo=UTC)) == "train" and X.period(datetime(2026, 9, 5, tzinfo=UTC)) == "test"
    assert X.period(datetime(2026, 10, 1, tzinfo=UTC)) is None


def _row(i, p, y, cat="sports", h=24, per="train", vol=5000.0, fee=0.05):
    return {"market": f"m{i}", "cluster": f"ev{i}", "category": cat, "horizon": h, "period": per, "p_mkt": p,
            "bucket": X.bucket(p), "outcome": y, "fee_rate": fee, "volume": vol}


def test_rule_trade_sides_costs_and_micro():
    t = X.rule_trade(_row(1, 0.05, 0), "no")
    assert t.side == "no" and t.cost == pytest.approx(0.95 + 0.015) and t.pnl > 0
    t = X.rule_trade(_row(1, 0.95, 1), "yes")
    assert t.side == "yes" and t.pnl == pytest.approx(1 - 0.965 - 0.05 * 0.965 * 0.035)
    assert X.rule_trade(_row(1, 0.05, 0, vol=500), "no") is None


def test_select_rules_needs_min_trades_and_positive_roi():
    good = [_row(i, 0.05, 0) for i in range(40)]                      # аутсайдеры не сыграли → NO прибылен
    bad = [_row(100 + i, 0.05, int(i < 10), cat="crypto") for i in range(40)]  # 25% сыграли → NO убыточен
    few = [_row(200 + i, 0.05, 0, cat="tech") for i in range(10)]
    rules = X.select_rules(good + bad + few)
    assert set(rules) == {("long", "sports", 24)}


def test_verdict_go_needs_30_trades_and_ci_above_zero():
    win = [(_row(i, 0.05, 0), X.rule_trade(_row(i, 0.05, 0), "no")) for i in range(40)]
    v = X.verdict(win, boot=200)
    assert v["go"] and v["trades"] == 40
    assert not X.verdict(win[:20], boot=200)["go"]
    mixed = [(_row(i, 0.05, int(i % 10 == 0)), X.rule_trade(_row(i, 0.05, int(i % 10 == 0)), "no")) for i in range(40)]
    assert not X.verdict(mixed, boot=200)["go"]
    # ROI > 0, но 4 кластера и один проигрыш: интервал захватывает ноль → не продолжаем
    one = [(dict(_row(i, 0.05, int(i == 0)), cluster=f"c{i % 4}"), X.rule_trade(_row(i, 0.05, int(i == 0)), "no"))
           for i in range(40)]
    v = X.verdict(one, boot=400)
    assert v["roi"] > 0 and v["ci"][0] < 0 and not v["go"]


def _market(mid, end, closed=None, hist=None, cat="sports", event="1", volume=5000.0, outcome=0, start=None):
    start = start or end - timedelta(days=10)
    m = S.SeriesMarket(id=mid, question="Will X?", slug="x", description="", start=start, closed=closed or end,
                       volume=volume, liquidity=None, fee_rate=0.05, neg_risk=False, yes_token="y", outcome=outcome,
                       history=hist or [], event_id=event, cls=cat, end_planned=end)
    return m


def test_a_rows_from_planned_end_fresh_price_and_open_market():
    R = _runner()
    end = datetime(2026, 8, 20, 12, tzinfo=UTC)
    hist = [(int((end - timedelta(hours=h)).timestamp()), 0.05) for h in range(200, 0, -1)]
    rows = R.a_rows([_market("a", end, hist=hist)])
    assert {r["horizon"] for r in rows} == set(X.HORIZONS_H) and all(r["period"] == "train" for r in rows)
    early = R.a_rows([_market("b", end, closed=end - timedelta(hours=30), hist=hist)])
    assert {r["horizon"] for r in early} == {72, 168}                   # закрыт до t — точки 6 и 24 ч нет
    stale = [(int((end - timedelta(hours=200)).timestamp()), 0.05)]
    assert R.a_rows([_market("c", end, hist=stale)]) == []              # котировка застыла — не цена


def test_cmd_a_selects_rules_on_train_only(tmp_path, monkeypatch):
    """Правило, прибыльное только в сентябре, на обучении не отбирается — проверка без подгонки."""
    R = _runner()
    monkeypatch.setattr(B, "data_dir", lambda: tmp_path)
    ms = []
    for i in range(40):  # июль: аутсайдер «Да» сыграл в 10% — NO убыточен
        end = datetime(2026, 7, 10, tzinfo=UTC) + timedelta(hours=i)
        ms.append(_market(f"j{i}", end, outcome=int(i % 10 == 0), event=f"j{i}",
                          hist=[(int((end - timedelta(hours=h)).timestamp()), 0.05) for h in range(200, 0, -1)]))
    for i in range(120):  # сентябрь: не сыграл ни разу — вместе с июлем (2.5%) NO был бы прибылен
        end = datetime(2026, 9, 10, tzinfo=UTC) + timedelta(hours=i)
        ms.append(_market(f"s{i}", end, outcome=0, event=f"s{i}",
                          hist=[(int((end - timedelta(hours=h)).timestamp()), 0.05) for h in range(200, 0, -1)]))
    (tmp_path / "biases").mkdir()
    (tmp_path / "biases" / "markets.jsonl").write_text("".join(m.to_json() + "\n" for m in ms))
    R.cmd_a(None)
    assert json.loads((tmp_path / "biases" / "a_rules.json").read_text()) == {}


# ─────────────────────────── В ─────────────────────────────────────
def _leg(p, outcome, vol=50_000.0, group="g", t0=1_000_000):
    """Живой ряд: цена менялась за сутки до окна (иначе это котировка пустого стакана)."""
    return {"history": [(t0 - 3600, p + 0.01), (t0, p)], "volume": vol, "fee_rate": 0.0, "outcome": outcome,
            "group": group}


def test_negrisk_windows_sum_over_and_exhaustive_only():
    legs = [_leg(0.5, 1), _leg(0.4, 0), _leg(0.3, 0)]                   # сумма 1.2
    w = X.negrisk_windows(legs, [1_000_100])
    assert len(w) == 1 and w[0].kind == "negrisk_over" and w[0].edge == pytest.approx(0.2 - 3 * B.SPREAD["mid"] / 2)
    assert X.negrisk_windows([_leg(0.5, 0), _leg(0.4, 0)], [1_000_100]) == []  # нет исхода «Да» — список неполный
    assert X.negrisk_windows([_leg(0.5, 1), _leg(0.5, 0)], [1_000_100]) == []  # сумма 1 — окна нет
    assert X.negrisk_windows([_leg(0.5, 1, vol=100), _leg(0.8, 0)], [1_000_100]) == []  # микро-нога неисполнима


def test_ladder_windows_monotonicity():
    d1, d2 = datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
    s = [dict(_leg(0.6, 0), date=d1), dict(_leg(0.4, 1), date=d2)]     # P(к 31.08) > P(к 30.09) — нарушение
    w = X.ladder_windows(s, [1_000_100])
    assert len(w) == 1 and w[0].edge == pytest.approx(0.2 - 2 * B.SPREAD["mid"] / 2)
    ok = [dict(_leg(0.3, 0), date=d1), dict(_leg(0.5, 1), date=d2)]
    assert X.ladder_windows(ok, [1_000_100]) == []
    assert X.ladder_date("September 30", "Will X happen by September 30?", 2026) == d2
    assert X.ladder_date("September 30", "Will X happen on September 30?", 2026) is None


def test_price_at_and_dedupe():
    h = [(100, 0.2), (200, 0.3)]
    assert X.price_at(h, 199) == 0.2 and X.price_at(h, 200) == 0.3 and X.price_at(h, 99) is None
    assert X.price_at(h, 200 + 3 * 3600 + 1) is None                    # старше 3 ч — не цена
    ws = [X.Window("ladder", "g", t, 0.1, 2, 1e4) for t in (0, 3600, 7 * 3600)]
    assert [w.ts for w in X.dedupe_windows(ws)] == [0, 7 * 3600]


# ─────────────────────────── Б ─────────────────────────────────────
def test_event_study_and_first_per_window():
    hist = [(t, 0.3 + 0.001 * (t // 60)) for t in range(0, 4 * H, 60)]
    path = X.event_study(H, hist)
    assert set(path) == set(X.DELAYS_MIN) and path[60] > path[0]
    assert X.event_study(H, [(0, 0.3)]) is None                          # нет свежей цены — нет точки
    assert X.first_per_window([("m", 0), ("m", 1000), ("m", 4000), ("k", 10)]) == [("k", 10), ("m", 0), ("m", 4000)]


def test_speed_trade_exit_and_resolution():
    path = {0: 0.30, 2: 0.31, 30: 0.40, 60: 0.35}
    t = X.speed_trade(path, 0, 2, 30, 5000.0, 0.0)
    assert t.pnl == pytest.approx(0.40 - 0.015 - (0.31 + 0.015))
    t = X.speed_trade(path, 1, 2, None, 5000.0, 0.0)
    assert t.pnl == pytest.approx(1 - 0.325)


# ─────────────────────────── Г ─────────────────────────────────────
def test_yes_view_mirrors_no_token():
    assert X.yes_view({"timestamp": 5, "side": "BUY", "price": 0.7, "size": 10, "asset": "y"}, "y") == (5, "buy", 0.7, 10)
    assert X.yes_view({"timestamp": 5, "side": "BUY", "price": 0.7, "size": 10, "asset": "n"}, "y") == \
        (5, "sell", pytest.approx(0.3), 10)
    assert X.yes_view({"side": "BUY"}, "y") is None


def test_simulate_quotes_uses_mid_strictly_before_trade():
    mids = [(100, 0.40), (200, 0.90)]                                    # середина прыгнула В момент сделки
    fills = X.simulate_quotes([(200, "buy", 0.93, 30)], mids, 0.03, 50)
    assert fills == [{"ts": 200, "we": "sell", "px": pytest.approx(0.43), "qty": 30, "mid": 0.40}]
    assert X.simulate_quotes([(200, "buy", 0.42, 30)], mids, 0.03, 50) == []   # внутри нашего ask — не наш
    assert X.simulate_quotes([(150, "sell", 0.30, 80)], mids, 0.03, 50)[0]["qty"] == 50


def test_markout_and_resolution_signs():
    f = {"ts": 100, "we": "sell", "px": 0.43, "qty": 10, "mid": 0.4}
    assert X.markout(f, [(100, 0.4), (400, 0.9)], 5) == pytest.approx(-0.47)
    assert X.to_resolution(f, 1) == pytest.approx(-0.57) and X.to_resolution(dict(f, we="buy"), 1) == pytest.approx(0.57)


def test_data_api_whitelisted():
    from forecast_bot.polymarket import http

    assert "data-api.polymarket.com" in http.ALLOWED_HOSTS


# ─────────────────────────── Д и В+ (ИИ) ────────────────────────────
def test_implication_windows():
    a, b = dict(_leg(0.6, 0)), dict(_leg(0.4, 1))                     # P(a) > P(b), а «a ⇒ b»
    w = X.implication_windows(a, b, [1_000_100], "a>b")
    assert len(w) == 1 and w[0].kind == "implied" and w[0].edge == pytest.approx(0.2 - 2 * B.SPREAD["mid"] / 2)
    assert X.implication_windows(dict(_leg(0.3, 0)), dict(_leg(0.5, 1)), [1_000_100], "x") == []


def _text_market(i, end, cat="politics", event=None, q=None):
    m = _market(f"t{i}", end, cat=cat, event=event or f"e{i}",
                hist=[(int((end - timedelta(hours=h)).timestamp()), 0.05) for h in range(200, 0, -1)])
    m.question = q or f"Will Zelensky meet Trump by September {i % 28 + 1}?"
    m.description = "This market resolves YES only if the official White House schedule lists the meeting. " * 2
    return m


def _d_fixture(tmp_path, monkeypatch, fake_llm, answer):
    from forecast_bot import ai_guard, guarded_llm

    R = _runner()
    monkeypatch.setattr(B, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(guarded_llm, "APP", "polymarket")
    monkeypatch.setattr(fake_llm, "_answer", staticmethod(answer))
    (tmp_path / "biases").mkdir()
    ms = [_text_market(i, datetime(2026, 9, 10, tzinfo=UTC) + timedelta(hours=i)) for i in range(6)]
    (tmp_path / "biases" / "markets.jsonl").write_text("".join(m.to_json() + "\n" for m in ms))
    return R, ms, ai_guard


def test_d_labels_through_guard_and_caps(tmp_path, monkeypatch, fake_llm, capsys):
    import asyncio
    import types

    R, ms, ai_guard = _d_fixture(tmp_path, monkeypatch, fake_llm,
                                 lambda t: '{"trap": true, "kind": "source", "direction": "harder", "reason": "r"}')
    monkeypatch.setattr(R, "STAGE4_START", datetime(2026, 1, 1, tzinfo=UTC))  # тест не зависит от часов
    asyncio.run(R._run_d(types.SimpleNamespace(n=10)))
    labs = [json.loads(l) for l in (tmp_path / "biases" / "d_labels.jsonl").read_text().splitlines()]
    assert len(labs) == 6 and all(l["trap"] and l["direction"] == "harder" for l in labs)
    assert R.stage_spent(":d") > 0 and R.stage_spent(":c2") == 0           # учёт в леджере под pm4:d
    conn = ai_guard._conn()
    conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                 (R.STAGE4_START.timestamp() + 60, "openrouter", "m", "polymarket:pm4:d:x", 0, 0, 1.0))
    conn.commit(); conn.close()
    (tmp_path / "biases" / "d_labels.jsonl").unlink()
    n_before = len(fake_llm.prompts)
    asyncio.run(R._run_d(types.SimpleNamespace(n=10)))
    assert len(fake_llm.prompts) == n_before                              # потолок Д $1 — ни одного вызова
    assert "потолок Д / этапа — стоп" in capsys.readouterr().out          # стоп именно по потолку, явно


def test_stage4_cap_counts_only_pm4(tmp_path, monkeypatch):
    from forecast_bot import ai_guard

    R = _runner()
    conn = ai_guard._conn()
    for user, cost in (("polymarket:pm3:1-t48-tg", 14.0), ("polymarket:pm4:c2:1", 0.5)):
        conn.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
                     (R.STAGE4_START.timestamp() + 60, "openrouter", "m", user, 0, 0, cost))
    conn.commit(); conn.close()
    assert R.stage_spent() == pytest.approx(0.5) and R.stage_spent(":c2") == pytest.approx(0.5)


def test_c2_groups_distinct_events_same_month():
    R = _runner()
    end = datetime(2026, 9, 10, tzinfo=UTC)
    ms = [_text_market(i, end, event="same") for i in range(5)]             # одно событие — не группа
    ms += [_text_market(10 + i, end) for i in range(4)]
    ms += [_text_market(20, end, cat="sports")]
    gs = R.c2_groups(ms, 10)
    assert gs and all(len({m.event_id for m in g}) == len(g) and all(m.cls != "sports" for m in g) for g in gs)


def test_frozen_quotes_are_not_arbitrage():
    """Все исходы матча стоят по 0.50 неделю — сумма 1.5, но это пустой стакан, а не цены."""
    frozen = [{"history": [(1_000_000 - k * 3600, 0.5) for k in range(29, -1, -1)], "volume": 50_000.0, "fee_rate": 0.0,
               "outcome": int(i == 0), "group": "g"} for i in range(3)]
    assert X.negrisk_windows(frozen, [1_000_100]) == []
    assert X.active_price([(1_000_000 - 7200, 0.4), (1_000_000, 0.45)], 1_000_100) == 0.45


def test_trade_price_windows_need_recent_trades_on_every_leg():
    legs = [{"history": [(1_000_000, 0.5)], "volume": 50_000.0, "fee_rate": 0.0, "outcome": 1, "group": "g"},
            {"history": [(1_000_000, 0.6)], "volume": 50_000.0, "fee_rate": 0.0, "outcome": 0, "group": "g"}]
    assert len(X.negrisk_windows(legs, [1_000_100], price=X.trade_price)) == 1
    assert X.negrisk_windows(legs, [1_000_000 + 3 * 3600], price=X.trade_price) == []   # сделки старше 2 ч


def test_ladder_needs_later_date_and_same_question():
    d = datetime(2026, 7, 31, tzinfo=UTC)
    same_date = [dict(_leg(0.6, 0), date=d, tmpl=X.ladder_template("Will K hit $11B by July 31?")),
                 dict(_leg(0.3, 0), date=d, tmpl=X.ladder_template("Will K hit $15B by July 31?"))]
    assert X.ladder_windows(same_date, [1_000_100]) == []               # порог, а не дата — не лестница
    d2 = datetime(2026, 8, 31, tzinfo=UTC)
    other_q = [dict(_leg(0.6, 0), date=d, tmpl=X.ladder_template("Will A happen by July 31?")),
               dict(_leg(0.3, 0), date=d2, tmpl=X.ladder_template("Will B happen by August 31?"))]
    assert X.ladder_windows(other_q, [1_000_100]) == []
    ok = [dict(_leg(0.6, 0), date=d, tmpl=X.ladder_template("Will A happen by July 31?")),
          dict(_leg(0.3, 1), date=d2, tmpl=X.ladder_template("Will A happen by August 31?"))]
    assert len(X.ladder_windows(ok, [1_000_100])) == 1
