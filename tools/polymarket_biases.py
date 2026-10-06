"""Этап 4 Polymarket: А — перекосы цены, В — несогласованность, Б — скорость (Украина). Без ИИ, только чтение.

    .venv/bin/python tools/polymarket_biases.py select     # общая выборка: до K рынков на (день × категорию), ≥ $1k
    .venv/bin/python tools/polymarket_biases.py a          # А: обучение июль–август → проверка сентябрь
    .venv/bin/python tools/polymarket_biases.py c          # В: negRisk-суммы и лестницы «к дате»
    .venv/bin/python tools/polymarket_biases.py b          # Б: пост в папке → минутная цена рынка
    .venv/bin/python tools/polymarket_biases.py g          # Г: котировки внутри спреда по истории сделок
    .venv/bin/python tools/polymarket_biases.py d          # Д: ИИ читает правила — ловушки закрытия (≤ $1)
    .venv/bin/python tools/polymarket_biases.py c2         # В+: связи между рынками находит ИИ по тексту (≤ $1)
    .venv/bin/python tools/polymarket_biases.py report [--out файл.md]

Данные — ~/.forecast-bot/polymarket/biases/ (вне репо). ИИ — только Д и В+: дешёвая модель через ai_guard,
приложение `polymarket`, пользователи `pm4:*`, потолок этапа $3; суточный лимит приложения НЕ поднимается.
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "polymarket"  # ДО импорта forecast_bot: потолок приложения читается при импорте

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot.polymarket import backtest as B, biases as X, markets as M, series as S  # noqa: E402
from forecast_bot.polymarket import series_backtest as SB  # noqa: E402
from forecast_bot.polymarket.http import get_json  # noqa: E402

UTC = timezone.utc
END_MIN = datetime(2026, 7, 1, tzinfo=UTC)
END_MAX = datetime(2026, 10, 1, tzinfo=UTC)
PER_DAY_CAT = 6
MIN_VOLUME = 1000
WINDOW_H = 6
C_EVENTS = 250          # событий на тип (negRisk / лестница) — по хэшу id
C_MAX_LEGS = 25


def d() -> Path:
    p = B.data_dir() / "biases"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def write_jsonl(p: Path, rows) -> None:
    with p.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def to_market(raw: dict):
    """Бинарный Yes/No с однозначным итогом, любой срок от 6 ч. Поля этапа: cls=категория, key=negRisk-группа,
    rule=groupItemTitle, event_id, end_planned."""
    base = M.from_gamma(raw, max_life_days=10_000, min_life_days=0.25)
    end = M.parse_ts(raw.get("endDate"))
    if base is None or end is None:
        return None
    sm = S.SeriesMarket(**{k: getattr(base, k) for k in base.__dataclass_fields__})
    evs = raw.get("events") or [{}]
    sm.event_id, sm.event_title = str(evs[0].get("id") or ""), evs[0].get("title") or ""
    sm.cls, sm.key = X.category(raw.get("feeType")), raw.get("negRiskMarketID") or ""
    sm.end_planned, sm.rule = end, raw.get("groupItemTitle") or ""
    return sm


def load_markets(name="markets.jsonl") -> list[S.SeriesMarket]:
    p = d() / name
    return [S.SeriesMarket.from_json(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


# ─────────────────────────── select ────────────────────────────────
def cmd_select(_a) -> int:
    """Проход 1: все рынки ≥ $1k окнами по 6 ч (для В — список событий). Проход 2: история цен отобранных."""
    cand_p, ev_p = d() / "candidates.jsonl", d() / "events.jsonl"
    if not cand_p.exists():
        cands, events, trunc = [], {}, 0
        w = END_MAX
        while w > END_MIN:
            w0 = w - timedelta(hours=WINDOW_H)
            for off in range(0, M.MAX_OFFSET + 1, 100):
                try:
                    rows = get_json(M.GAMMA, {"closed": "true", "limit": 100, "offset": off,
                                              "volume_num_min": MIN_VOLUME,
                                              "end_date_min": w0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                              "end_date_max": w.strftime("%Y-%m-%dT%H:%M:%SZ")})
                except RuntimeError as exc:
                    print(f"{w0:%m-%d %H}: {str(exc)[:80]}", flush=True)
                    break
                for raw in rows:
                    ev = (raw.get("events") or [{}])[0]
                    if ev.get("id"):
                        events.setdefault(str(ev["id"]), {"id": str(ev["id"]), "title": ev.get("title") or "",
                                                          "negRisk": bool(raw.get("negRisk"))})
                    m = to_market(raw)
                    if m:
                        cands.append({"raw": {k: raw.get(k) for k in (
                            "id", "question", "slug", "description", "startDate", "endDate", "closedTime", "volumeNum",
                            "liquidityNum", "outcomes", "outcomePrices", "clobTokenIds", "umaResolutionStatus",
                            "feeType", "feesEnabled", "negRisk", "negRiskMarketID", "groupItemTitle")} |
                            {"events": [{"id": ev.get("id"), "title": ev.get("title")}]},
                            "day": m.end_planned.strftime("%Y-%m-%d"), "cat": m.cls})
                if len(rows) < 100:
                    break
                if off + 100 > M.MAX_OFFSET:
                    trunc += 1
            w = w0
            if w.hour == 0:
                print(f"{w:%Y-%m-%d}: кандидатов {len(cands)}, событий {len(events)}", flush=True)
        write_jsonl(cand_p, cands)
        write_jsonl(ev_p, events.values())
        print(f"окон с упором в offset 2000: {trunc}", flush=True)
    cands = load_jsonl(cand_p)
    by = defaultdict(list)
    for c in cands:
        by[(c["day"], c["cat"])].append(c)
    pick = [c for k in sorted(by) for c in sorted(by[k], key=lambda c: X.sample_rank(c["raw"]["id"]))[:PER_DAY_CAT]]
    have = {m.id for m in load_markets()}
    todo = [c for c in pick if str(c["raw"]["id"]) not in have]
    print(f"кандидатов {len(cands)}, отобрано {len(pick)}, качать историй {len(todo)}", flush=True)
    with (d() / "markets.jsonl").open("a") as fh:
        for i, c in enumerate(todo, 1):
            m = to_market(c["raw"])
            try:
                m.history = M.load_history(m)
            except Exception as exc:
                print(f"история {m.id}: {type(exc).__name__}", flush=True)
                continue
            if len(m.history) >= 3:
                fh.write(m.to_json() + "\n")
            if i % 500 == 0:
                print(f"история {i}/{len(todo)}", flush=True)
    return 0


# ─────────────────────────── А ─────────────────────────────────────
def a_rows(markets: list[S.SeriesMarket]) -> list[dict]:
    out = []
    for m in markets:
        per = X.period(m.end_planned)
        if per is None:
            continue
        for h in X.HORIZONS_H:
            t = m.end_planned - timedelta(hours=h)
            if t <= m.start + timedelta(hours=1) or m.closed <= t:
                continue  # рынка ещё не было / уже решён на t
            px = S.price_point(m, t)
            if px is None or (t - px[0]).total_seconds() > 3 * 3600:
                continue  # нет свежей цены — котировка застыла
            out.append({"market": m.id, "cluster": f"ev{m.event_id or m.id}", "category": m.cls, "horizon": h,
                        "period": per, "p_mkt": px[1], "bucket": X.bucket(px[1]), "outcome": m.outcome,
                        "fee_rate": m.fee_rate, "volume": m.volume})
    return out


def cmd_a(_a) -> int:
    rows = a_rows(load_markets())
    write_jsonl(d() / "a_rows.jsonl", rows)
    train = [r for r in rows if r["period"] == "train"]
    rules = X.select_rules(train)
    (d() / "a_rules.json").write_text(json.dumps({"|".join(map(str, k)): v for k, v in rules.items()}, indent=1))
    print(f"строк {len(rows)} (обучение {len(train)}), правил отобрано на обучении: {len(rules)}")
    return 0


def a_report(boot: int) -> list[str]:
    rows = load_jsonl(d() / "a_rows.jsonl")
    rules = {tuple(k.split("|")[:2]) + (int(k.split("|")[2]),): v for k, v in
             json.loads((d() / "a_rules.json").read_text()).items()}
    test = [r for r in rows if r["period"] == "test"]
    out = ["### А. Калибровка крайних цен (все периоды, рынки ≥ $1k)", "",
           "| корзина | категория | n | событий | средняя цена YES | доля «Да» |", "|---|---|---|---|---|---|"]
    cal = defaultdict(list)
    for r in rows:
        if r["bucket"]:
            cal[(r["bucket"], r["category"])].append(r)
            cal[(r["bucket"], "все")].append(r)
    for k in sorted(cal, key=lambda k: (k[0], k[1] == "все", -len(cal[k]))):
        rs = cal[k]
        if len(rs) < 30 and k[1] != "все":
            continue
        out.append(f"| {k[0]} | {k[1]} | {len(rs)} | {len({r['cluster'] for r in rs})} | "
                   f"{sum(r['p_mkt'] for r in rs) / len(rs):.3f} | {sum(r['outcome'] for r in rs) / len(rs):.3f} |")
    out += ["", f"Правил отобрано на обучении (июль–август, ROI > 0, ≥ {X.MIN_TRADES} сделок): {len(rules)}", ""]
    out += ["| правило | обучение: сделок / ROI | проверка (сентябрь): сделок | событий | ROI | ROI 90% |",
            "|---|---|---|---|---|---|"]
    for k, v in sorted(rules.items()):
        vt = X.verdict(X.apply_rules(test, [k]), boot)
        out.append(f"| {k[0]}→{X.RULE_SIDE[k[0]].upper()} · {k[1]} · {k[2]} ч | {v['n']} / {v['roi']:+.1%} | "
                   f"{vt['trades']} | {vt['events']} | {_pct(vt['roi'])} | {_ci(vt['ci'])} |")
    all_v = X.verdict(X.apply_rules(test, rules), boot)
    out += ["", f"**Итог А на проверке (все отобранные правила вместе)**: сделок {all_v['trades']}, событий "
            f"{all_v['events']}, ROI {_pct(all_v['roi'])}, 90% {_ci(all_v['ci'])} → "
            f"{'ПРОДОЛЖАЕМ' if all_v['go'] else 'стоп'}"]
    for b in ("long", "fav"):  # контроль без отбора: правило на всех категориях и сроках
        allc = {c for c in {X.cell(r) for r in rows if r["bucket"] == b}}
        v = X.verdict(X.apply_rules(test, allc), boot)
        out.append(f"- контроль «{b}→{X.RULE_SIDE[b].upper()} везде» на проверке: сделок {v['trades']}, "
                   f"ROI {_pct(v['roi'])}, 90% {_ci(v['ci'])}")
    return out


def _pct(x) -> str:
    return f"{x:+.1%}" if x is not None else "—"


def _ci(ci) -> str:
    return f"[{ci[0]:+.0%}; {ci[1]:+.0%}]" if ci else "—"


# ─────────────────────────── В ─────────────────────────────────────
def c_events() -> tuple[list[dict], list[dict]]:
    evs = load_jsonl(d() / "events.jsonl")
    neg = sorted([e for e in evs if e["negRisk"]], key=lambda e: X.sample_rank(e["id"]))[:C_EVENTS]
    lad = sorted([e for e in evs if not e["negRisk"] and "by" in e["title"].lower()],
                 key=lambda e: X.sample_rank(e["id"]))[:C_EVENTS]
    return neg, lad


def cmd_c(_a) -> int:
    neg, lad = c_events()
    cache_p = d() / "c_events.jsonl"
    done = {r["id"] for r in load_jsonl(cache_p)}
    with cache_p.open("a") as fh:
        for kind, evs in (("neg", neg), ("ladder", lad)):
            for e in evs:
                if e["id"] in done:
                    continue
                try:
                    full = get_json(f"https://gamma-api.polymarket.com/events/{e['id']}")
                except RuntimeError:
                    continue
                legs = []
                for raw in (full.get("markets") or [])[:C_MAX_LEGS]:
                    m = to_market(raw | {"events": [{"id": e["id"], "title": e["title"]}]})
                    if m is None:
                        continue
                    try:
                        m.history = M.load_history(m)
                    except Exception:
                        continue
                    legs.append(json.loads(m.to_json()))
                n_all = len(full.get("markets") or [])
                fh.write(json.dumps({"id": e["id"], "kind": kind, "title": e["title"], "n_markets": n_all,
                                     "legs": legs}, ensure_ascii=False) + "\n")
                fh.flush()
    windows = []
    for ev in load_jsonl(cache_p):
        legs = [S.SeriesMarket.from_json(json.dumps(x)) for x in ev["legs"]]
        if len(legs) < 2:
            continue
        lo = max(m.start for m in legs)
        hi = min(m.closed for m in legs)
        hours = list(range(int(lo.timestamp()) // 3600 * 3600 + 3600, int(hi.timestamp()), 3600))
        if ev["kind"] == "neg" and len(legs) == ev["n_markets"]:
            mem = [{"history": m.history, "volume": m.volume, "fee_rate": m.fee_rate, "outcome": m.outcome,
                    "group": ev["id"]} for m in legs]
            windows += X.negrisk_windows(mem, hours)
        elif ev["kind"] == "ladder":
            steps = []
            for m in legs:
                dt = X.ladder_date(m.rule, m.question, m.end_planned.year)
                if dt:
                    steps.append({"date": dt, "history": m.history, "volume": m.volume, "fee_rate": m.fee_rate,
                                  "group": ev["id"]})
            if len(steps) >= 2:
                windows += X.ladder_windows(steps, hours)
    ws = X.dedupe_windows(windows)
    write_jsonl(d() / "c_windows.jsonl", [w.__dict__ for w in ws])
    print(f"окон после склейки: {len(ws)}; по типам: {dict(Counter(w.kind for w in ws))}")
    # Проверка по РЕАЛЬНЫМ сделкам: середина CLOB у неликвидных исходов — котировка пустого стакана. Для событий с
    # окнами качаем сделки всех ног и пересчитываем окна по цене последней сделки (не старше 2 ч).
    verified = []
    for ev in load_jsonl(cache_p):
        if ev["id"] not in {w.group for w in ws}:
            continue
        legs = [S.SeriesMarket.from_json(json.dumps(x)) for x in ev["legs"]]
        for m in legs:
            m.history = trade_series(m)
        lo, hi = max(m.start for m in legs), min(m.closed for m in legs)
        hours = list(range(int(lo.timestamp()) // 3600 * 3600 + 3600, int(hi.timestamp()), 3600))
        if ev["kind"] == "neg":
            mem = [{"history": m.history, "volume": m.volume, "fee_rate": m.fee_rate, "outcome": m.outcome,
                    "group": ev["id"]} for m in legs]
            verified += [w for w in X.negrisk_windows(mem, hours, price=X.trade_price)]
        else:
            steps = [{"date": dt, "history": m.history, "volume": m.volume, "fee_rate": m.fee_rate, "group": ev["id"]}
                     for m in legs if (dt := X.ladder_date(m.rule, m.question, m.end_planned.year))]
            verified += X.ladder_windows(steps, hours, price=X.trade_price)
    vs = X.dedupe_windows(verified)
    write_jsonl(d() / "c_windows_trades.jsonl", [w.__dict__ for w in vs])
    print(f"по реальным сделкам: {len(vs)}; по типам: {dict(Counter(w.kind for w in vs))}")
    return 0


def trade_series(m) -> list[tuple[int, float]]:
    """Цена «Да» по сделкам data-api (кэш на рынок; кошельки не сохраняются)."""
    p = d() / "trades" / f"{m.id}.json"
    if p.exists():
        return [tuple(x) for x in json.loads(p.read_text())]
    p.parent.mkdir(exist_ok=True)
    meta = get_json(f"https://gamma-api.polymarket.com/markets/{m.id}")
    out = []
    for off in range(0, 10_000, 500):
        try:
            page = get_json("https://data-api.polymarket.com/trades", {"market": meta.get("conditionId"), "limit": 500,
                                                                       "offset": off})
        except RuntimeError:
            break
        out += [(v[0], v[2]) for t in page if (v := X.yes_view(t, m.yes_token))]
        if len(page) < 500:
            break
    out.sort()
    p.write_text(json.dumps(out))
    return out


def c_report() -> list[str]:
    evs = load_jsonl(d() / "c_events.jsonl")
    ws = load_jsonl(d() / "c_windows.jsonl")
    neg_full = sum(1 for e in evs if e["kind"] == "neg" and len(e["legs"]) == e["n_markets"] and len(e["legs"]) >= 2)
    lad = sum(1 for e in evs if e["kind"] == "ladder" and len(e["legs"]) >= 2)
    out = ["### В. Несогласованность (цены — история CLOB, издержки — полспреда + fee на каждую ногу)", "",
           f"Проверено событий: negRisk с полным списком исходов {neg_full}, лестниц «к дате» {lad}.", "",
           "| тип | окон | событий | сентябрь: окон | медиана прибыли на комплект | p90 | мин. объём ноги, медиана |",
           "|---|---|---|---|---|---|---|"]
    for kind in ("negrisk_over", "negrisk_under", "ladder"):
        k = [w for w in ws if w["kind"] == kind]
        if not k:
            out.append(f"| {kind} | 0 | 0 | 0 | — | — | — |")
            continue
        e = sorted(w["edge"] for w in k)
        v = sorted(w["min_volume"] for w in k)
        sep = sum(1 for w in k if datetime.fromtimestamp(w["ts"], UTC).month == 9)
        out.append(f"| {kind} | {len(k)} | {len({w['group'] for w in k})} | {sep} | {e[len(e) // 2]:.3f} | "
                   f"{e[int(.9 * (len(e) - 1))]:.3f} | ${v[len(v) // 2]:,.0f} |")
    return out


# ─────────────────────────── Б ─────────────────────────────────────
def cmd_b(_a) -> int:
    from forecast_bot.polymarket import tg_news as T

    ua = B.data_dir() / "ua"
    markets = [S.SeriesMarket.from_json(l) for l in (ua / "markets.jsonl").read_text().splitlines() if l.strip()]
    kw = json.loads((ua / "keywords.json").read_text())
    posts = T.load_posts()
    items = []
    for m in markets:
        words = kw.get(m.event_id)
        if not words:
            continue
        lo, hi = m.start.timestamp(), m.closed.timestamp()
        for p in posts:
            ts = p.date.timestamp()
            if lo <= ts < hi - 3600 and T.score(p.text, words) >= 2:
                items.append((m.id, ts))
    ev = X.first_per_window(items)
    by_m = {m.id: m for m in markets}
    hist_p = d() / "b_minute.jsonl"
    cache = {(r["market"], r["day"]): r["history"] for r in load_jsonl(hist_p)}
    need = sorted({(mk, int(ts // 86400)) for mk, ts in ev} | {(mk, int(ts // 86400) + 1) for mk, ts in ev})
    with hist_p.open("a") as fh:
        for mk, day in need:
            if (mk, day) in cache:
                continue
            try:
                h = get_json(M.CLOB_HISTORY, {"market": by_m[mk].yes_token, "startTs": day * 86400,
                                              "endTs": (day + 1) * 86400, "fidelity": 1})
            except RuntimeError:
                h = {}
            pts = [(int(x["t"]), float(x["p"])) for x in (h or {}).get("history", [])]
            cache[(mk, day)] = pts
            fh.write(json.dumps({"market": mk, "day": day, "history": pts}) + "\n")
    rows = []
    for kind, pts in (("пост", ev), ("плацебо", [(mk, ts + 7 * 3600) for mk, ts in ev])):
        for mk, ts in pts:  # плацебо — то же время суток +7 ч: тот же рынок, без поста
            day = int(ts // 86400)
            hist = sorted(cache.get((mk, day), []) + cache.get((mk, day + 1), []))
            path = X.event_study(ts, hist)
            if path is None:
                continue
            m = by_m[mk]
            rows.append({"kind": kind, "market": mk, "cluster": f"ev{m.event_id}", "ts": ts,
                         "month": datetime.fromtimestamp(ts, UTC).month, "path": {str(k): v for k, v in path.items()},
                         "outcome": m.outcome, "volume": m.volume, "fee_rate": m.fee_rate})
    write_jsonl(d() / "b_rows.jsonl", rows)
    print(f"совпадений пост↔рынок {len(items)}, событий (первый пост в часе) {len(ev)}, со свежей минутной ценой: "
          f"{dict(Counter(r['kind'] for r in rows))}")
    return 0


def b_report(boot: int) -> list[str]:
    rows = load_jsonl(d() / "b_rows.jsonl")
    out = ["### Б. Скорость: пост в папке → минутная цена «Да» (сдвиг от момента поста, п.п.)", "",
           "| выборка | n | событий | +2 мин | +5 | +15 | +30 | +60 |", "|---|---|---|---|---|---|---|---|"]
    for kind in ("пост", "плацебо"):
        rs = [r for r in rows if r["kind"] == kind]
        if not rs:
            continue
        cells = []
        for dm in (2, 5, 15, 30, 60):
            dlt = [r["path"][str(dm)] - r["path"]["0"] for r in rs]
            cells.append(f"{100 * sum(dlt) / len(dlt):+.2f}")
        out.append(f"| {kind} | {len(rs)} | {len({r['cluster'] for r in rs})} | " + " | ".join(cells) + " |")
    out += ["", "| правило (покупка «Да» через 2 мин) | период | сделок | событий | ROI | ROI 90% |",
            "|---|---|---|---|---|---|"]
    for kind in ("пост", "плацебо"):  # плацебо: та же сделка без поста — отделяет новость от перекоса самих рынков
        posts = [r for r in rows if r["kind"] == kind]
        for exit_min, label in ((30, "выход через 30 мин"), (60, "выход через 60 мин"), (None, "до резолва")):
            for per, months in (("все", None), ("сентябрь", (9,))):
                rs = [r for r in posts if months is None or r["month"] in months]
                pairs = [(r, t) for r in rs if (t := X.speed_trade({int(k): v for k, v in r["path"].items()},
                                                                      r["outcome"], 2, exit_min, r["volume"],
                                                                      r["fee_rate"]))]
                v = X.verdict(pairs, boot)
                out.append(f"| {kind}: {label} | {per} | {v['trades']} | {v['events']} | {_pct(v['roi'])} | "
                           f"{_ci(v['ci'])} |")
    return out


# ─────────────────────────── ИИ этапа 4 (Д, В+) ─────────────────────
STAGE4_START = datetime(2026, 10, 6, 21, tzinfo=UTC)  # 07.10.2026 00:00 по Киеву
STAGE4_CAP_USD = 3.0
D_CAP_USD = 1.0
C2_CAP_USD = 1.0
LEDGER_PREFIX = "pm4"
AI_MODEL = "openrouter/anthropic/claude-haiku-4.5"
TEXT_CATS = {"politics", "culture", "geopolitics", "mentions", "tech", "economics", "finance", "none", "other"}


def stage_spent(part: str = "") -> float:
    from forecast_bot import ai_guard

    return ai_guard.app_cost_since(f"{B.APP}:{LEDGER_PREFIX}{part}", STAGE4_START.timestamp())


async def _ask(user: str, prompt: str, max_tokens: int) -> str:
    from forecast_bot import guarded_llm

    if guarded_llm.APP != B.APP:
        raise RuntimeError(f"guarded_llm.APP={guarded_llm.APP!r}, нужен {B.APP!r}")
    guarded_llm.install_sentinel()
    token = guarded_llm.CURRENT_USER.set(user)
    try:
        resp = await guarded_llm.guarded_completion(AI_MODEL, [{"role": "user", "content": prompt}],
                                                    max_tokens=max_tokens, temperature=0)
    finally:
        guarded_llm.CURRENT_USER.reset(token)
    return resp.choices[0].message.content or ""


def _json_tail(text: str):
    import re

    found = re.findall(r"[\[{].*[\]}]", text or "", re.S)
    return json.loads(found[-1]) if found else None


D_PROMPT = """Prediction market question: {q}
Resolution rules: {rules}

Is there a resolution TRAP: the market resolves by a narrow source, exact time window, specific wording or
technicality, so a trader reading only the title could misjudge the outcome?
Answer JSON only: {{"trap": true|false, "kind": "source|time|wording|none",
"direction": "harder|easier|neutral", "reason": "one short sentence"}}
direction = does the rule make YES HARDER or EASIER than the title alone suggests. Do not guess the outcome."""


def d_sample(markets: list[S.SeriesMarket], n: int) -> list[S.SeriesMarket]:
    pool = [m for m in markets if m.cls in TEXT_CATS and X.period(m.end_planned) and len(m.description) > 80]
    return sorted(pool, key=lambda m: X.sample_rank(m.id))[:n]


async def _run_d(a) -> int:
    from forecast_bot import ai_guard, guarded_llm

    guarded_llm.start_run(max(0.0, min(D_CAP_USD - stage_spent(":d"), STAGE4_CAP_USD - stage_spent())))
    path = d() / "d_labels.jsonl"
    done = {r["market"] for r in load_jsonl(path)}
    n_ok = 0
    with path.open("a") as fh:
        for m in d_sample(load_markets(), a.n):
            if m.id in done:
                continue
            if stage_spent(":d") >= D_CAP_USD or stage_spent() >= STAGE4_CAP_USD:
                print("потолок Д / этапа — стоп")
                break
            try:
                text = await _ask(f"{LEDGER_PREFIX}:d:{m.id}", D_PROMPT.format(q=m.question, rules=m.description[:1500]),
                                  200)
                lab = _json_tail(text)
                rec = {"market": m.id, "trap": bool(lab.get("trap")), "kind": str(lab.get("kind", "")),
                       "direction": str(lab.get("direction", "")), "reason": str(lab.get("reason", ""))[:200]}
            except ai_guard.BudgetExceeded as exc:
                print(f"лимит ai_guard — стоп: {exc}")
                break
            except Exception as exc:
                rec = {"market": m.id, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_ok += "error" not in rec
    print(f"размечено {n_ok}; потрачено на Д ${stage_spent(':d'):.3f}, на этап ${stage_spent():.3f}")
    return 0


def d_report(boot: int) -> list[str]:
    labels = {r["market"]: r for r in load_jsonl(d() / "d_labels.jsonl") if "error" not in r}
    rows = [r for r in load_jsonl(d() / "a_rows.jsonl") if r["market"] in labels]
    out = ["### Д. Ловушки правил закрытия (ИИ размечает правила; цена — CLOB, ≥ $1k)", "",
           f"Размечено рынков: {len(labels)}, из них «ловушка»: {sum(1 for v in labels.values() if v['trap'])} "
           f"({Counter(v['kind'] for v in labels.values() if v['trap']).most_common()}).", "",
           "| группа | срок | n | событий | средняя |цена − исход| |", "|---|---|---|---|---|"]
    for trap in (True, False):
        for h in X.HORIZONS_H:
            rs = [r for r in rows if labels[r["market"]]["trap"] is trap and r["horizon"] == h]
            if rs:
                err = sum(abs(r["p_mkt"] - r["outcome"]) for r in rs) / len(rs)
                out.append(f"| {'ловушка' if trap else 'обычный'} | {h} ч | {len(rs)} | "
                           f"{len({r['cluster'] for r in rs})} | {err:.3f} |")
    out += ["", "| правило (ловушка: «труднее» → NO, «легче» → YES) | срок | период | сделок | событий | ROI | 90% |",
            "|---|---|---|---|---|---|---|"]
    for h in (24, 72):
        for per in ("train", "test"):
            pairs = []
            for r in rows:
                lab = labels[r["market"]]
                if not lab["trap"] or lab["direction"] not in ("harder", "easier") or r["horizon"] != h or \
                        r["period"] != per:
                    continue
                t = X.rule_trade(r, "no" if lab["direction"] == "harder" else "yes")
                if t:
                    pairs.append((r, t))
            v = X.verdict(pairs, boot)
            out.append(f"| ловушка → по правилу | {h} ч | {'июль–август' if per == 'train' else 'сентябрь'} | "
                       f"{v['trades']} | {v['events']} | {_pct(v['roi'])} | {_ci(v['ci'])} |")
    return out


C2_PROMPT = """Below are prediction markets on related topics (index: question).
{items}

List pairs where YES on market i LOGICALLY IMPLIES YES on market j (if i resolves YES, j must resolve YES),
e.g. "X wins by 10+ points" ⇒ "X wins"; "happens by Aug 31" ⇒ "happens by Sep 30"; "candidate of party P wins" ⇒
"party P wins". Only strict logical implications given the question texts, not likely correlations.
Answer JSON only: [[i, j], ...] or []."""

_STOP = {"Will", "Which", "What", "Who", "When", "How", "The", "Yes", "January", "February", "March", "April", "May",
         "June", "July", "August", "September", "October", "November", "December", "Monday", "Tuesday",
         "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Week", "Election", "Price", "Market"}


def c2_groups(markets: list[S.SeriesMarket], max_groups: int, size: tuple[int, int] = (3, 20)) -> list[list]:
    """Группы по общему собственному имени (≥ 4 букв) внутри месяца конца; каждая — до 20 рынков, РАЗНЫЕ события."""
    import re

    by = defaultdict(dict)
    for m in markets:
        if m.cls not in TEXT_CATS:
            continue
        for w in set(re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", m.question)) - _STOP:
            by[(w, m.end_planned.strftime("%Y-%m"))].setdefault(m.event_id or m.id, m)
    groups = []
    for k in sorted(by, key=lambda k: X.sample_rank("|".join(k))):
        ms = list(by[k].values())
        if size[0] <= len(ms) <= size[1]:
            groups.append(ms)
        if len(groups) >= max_groups:
            break
    return groups


async def _run_c2(a) -> int:
    from forecast_bot import ai_guard, guarded_llm

    guarded_llm.start_run(max(0.0, min(C2_CAP_USD - stage_spent(":c2"), STAGE4_CAP_USD - stage_spent())))
    path = d() / "c2_pairs.jsonl"
    done = {r["group"] for r in load_jsonl(path)}
    with path.open("a") as fh:
        for ms in c2_groups(load_markets(), a.groups):
            gid = "|".join(sorted(m.id for m in ms))
            if gid in done:
                continue
            if stage_spent(":c2") >= C2_CAP_USD or stage_spent() >= STAGE4_CAP_USD:
                print("потолок В+ / этапа — стоп")
                break
            items = "\n".join(f"{i}: {m.question}" for i, m in enumerate(ms))
            try:
                pairs = _json_tail(await _ask(f"{LEDGER_PREFIX}:c2:{X.sample_rank(gid)}", C2_PROMPT.format(items=items),
                                              300)) or []
                pairs = [(ms[i].id, ms[j].id) for i, j in pairs if 0 <= i < len(ms) and 0 <= j < len(ms) and i != j]
                rec = {"group": gid, "pairs": pairs}
            except ai_guard.BudgetExceeded as exc:
                print(f"лимит ai_guard — стоп: {exc}")
                break
            except Exception as exc:
                rec = {"group": gid, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            fh.write(json.dumps(rec) + "\n")
    print(f"групп {len(load_jsonl(path))}; потрачено на В+ ${stage_spent(':c2'):.3f}, на этап ${stage_spent():.3f}")
    return 0


def c2_report() -> list[str]:
    by_id = {m.id: m for m in load_markets()}
    recs = [r for r in load_jsonl(d() / "c2_pairs.jsonl") if "error" not in r]
    pairs = [(a, b) for r in recs for a, b in r["pairs"] if a in by_id and b in by_id]
    wrong = sum(1 for a, b in pairs if by_id[a].outcome == 1 and by_id[b].outcome == 0)
    ws = []
    for a, b in pairs:
        ma, mb = by_id[a], by_id[b]
        lo, hi = max(ma.start, mb.start), min(ma.closed, mb.closed)
        hours = range(int(lo.timestamp()) // 3600 * 3600 + 3600, int(hi.timestamp()), 3600)
        legs = [{"history": m.history, "volume": m.volume, "fee_rate": m.fee_rate} for m in (ma, mb)]
        ws += X.implication_windows(legs[0], legs[1], hours, f"{a}>{b}")
    ws = X.dedupe_windows(ws)
    e = sorted(w.edge for w in ws)
    return ["### В+. Связи между разными событиями, найденные ИИ по тексту", "",
            f"Групп проверено {len(recs)}, пар «Да(i) ⇒ Да(j)» {len(pairs)}; ИИ ошибся в {wrong} "
            f"(i сыграл, j нет — связь ложная, окно по ней было бы убытком).",
            f"Окон P(i) > P(j) + издержки: {len(ws)} (пар {len({w.group for w in ws})}); медиана прибыли "
            f"{e[len(e) // 2]:.3f} на комплект." if ws else "Окон P(i) > P(j) + издержки: 0."]


# ─────────────────────────── Г ─────────────────────────────────────
G_DELTAS = (0.02, 0.03, 0.045)   # полуширина нашей котировки; 4.5¢ — max spread наград на этих рынках (06.10.2026)
G_SIZE = 50                       # долей на сторону (min_size наград 20–50)
TRADES_MAX = 10_000


def _g_market_data(m) -> dict:
    """conditionId, настройки наград (текущие — истории наград API не даёт), сделки в виде «Да», середина 5 мин."""
    meta = get_json(f"https://gamma-api.polymarket.com/markets/{m.id}")
    cid = meta.get("conditionId")
    rw = {}
    try:
        rw = (get_json(f"https://clob.polymarket.com/markets/{cid}") or {}).get("rewards") or {}
    except RuntimeError:
        pass
    trades = []
    for off in range(0, TRADES_MAX, 500):
        try:
            page = get_json("https://data-api.polymarket.com/trades", {"market": cid, "limit": 500, "offset": off})
        except RuntimeError:
            break
        trades += [v for t in page if (v := X.yes_view(t, m.yes_token))]  # кошельки не сохраняем
        if len(page) < 500:
            break
    mids = {}
    lo, hi = int(m.start.timestamp()), int(m.closed.timestamp())
    while lo < hi:
        end = min(hi, lo + 7 * 86400)
        try:
            h = get_json(M.CLOB_HISTORY, {"market": m.yes_token, "startTs": lo, "endTs": end, "fidelity": 5})
            mids.update({int(x["t"]): float(x["p"]) for x in (h or {}).get("history", [])})
        except RuntimeError:
            pass
        lo = end
    rate = sum(float(r.get("rewards_daily_rate") or 0) for r in (rw.get("rates") or []))
    return {"market": m.id, "cid": cid, "reward_rate": rate, "min_size": rw.get("min_size"),
            "max_spread": rw.get("max_spread"), "trades": trades, "mids": sorted(mids.items())}


def cmd_g(_a) -> int:
    ua = B.data_dir() / "ua"
    markets = [S.SeriesMarket.from_json(l) for l in (ua / "markets.jsonl").read_text().splitlines() if l.strip()]
    cache_p = d() / "g_data.jsonl"
    have = {r["market"] for r in load_jsonl(cache_p)}
    with cache_p.open("a") as fh:
        for i, m in enumerate(markets, 1):
            if m.id in have:
                continue
            try:
                fh.write(json.dumps(_g_market_data(m)) + "\n")
                fh.flush()
            except RuntimeError as exc:
                print(f"{m.id}: {str(exc)[:80]}", flush=True)
            if i % 20 == 0:
                print(f"{i}/{len(markets)}", flush=True)
    print(f"рынков с данными: {len(load_jsonl(cache_p))}")
    return 0


def g_report(boot: int) -> list[str]:
    ua = B.data_dir() / "ua"
    by_m = {m.id: m for m in (S.SeriesMarket.from_json(l) for l in (ua / "markets.jsonl").read_text().splitlines()
                              if l.strip())}
    data = load_jsonl(d() / "g_data.jsonl")
    out = ["### Г. Маркетмейкинг внутри спреда (рынки «Украина», история сделок data-api, середина — CLOB 5 мин)", "",
           f"Котировка середина ± δ по {G_SIZE} долей на сторону; заполнение — если сделка тейкера прошла через наш "
           "уровень (оптимистично: без очереди, котировка восстанавливается сразу; истории стакана API не даёт).", "",
           "| δ | рынков с заполнениями | событий | заполнений | долей | край к середине, ¢/доля | марк-аут 5 / 30 / 120 мин, "
           "¢/доля | до резолва, ¢/доля | итог до резолва, $ | 90% (по событиям), ¢/доля |", "|---|---|---|---|---|---|---|---|---|---|"]
    for delta in G_DELTAS:
        rows = []
        for g in data:
            m = by_m.get(g["market"])
            if m is None:
                continue
            mids = [tuple(x) for x in g["mids"]]
            for f in X.simulate_quotes([tuple(t) for t in g["trades"]], mids, delta, G_SIZE):
                mk = {n: X.markout(f, mids, n) for n in X.MARKOUT_MIN}
                rows.append({**f, "cluster": f"ev{m.event_id}", "market": m.id, "mk": mk,
                             "res": X.to_resolution(f, m.outcome)})
        if not rows:
            out.append(f"| {delta * 100:.1f}¢ | 0 | — | — | — | — | — | — | — | — |")
            continue
        q = sum(r["qty"] for r in rows)
        w = lambda key: sum(r["qty"] * key(r) for r in rows if key(r) is not None) / max(  # noqa: E731
            1e-9, sum(r["qty"] for r in rows if key(r) is not None))
        mks = " / ".join(f"{100 * w(lambda r, n=n: r['mk'][n]):+.2f}" for n in X.MARKOUT_MIN)
        res_total = sum(r["qty"] * r["res"] for r in rows)
        ci = SB.bootstrap(rows, lambda s: sum(r["qty"] * r["res"] for r in s) / sum(r["qty"] for r in s), boot)
        out.append(f"| {delta * 100:.1f}¢ | {len({r['market'] for r in rows})} | {len({r['cluster'] for r in rows})} | "
                   f"{len(rows)} | {q:,.0f} | {100 * w(lambda r: r['px'] - r['mid'] if r['we'] == 'sell' else r['mid'] - r['px']):+.2f} | "
                   f"{mks} | {100 * res_total / q:+.2f} | {res_total:+,.0f} | "
                   f"{'[%+.1f; %+.1f]' % (100 * ci[0], 100 * ci[1]) if ci else '—'} |")
    days = sum((by_m[g["market"]].closed - by_m[g["market"]].start).total_seconds() / 86400 for g in data
               if g["market"] in by_m)
    if rows:
        out += ["", f"Чтобы δ = {G_DELTAS[-1] * 100:.1f}¢ вышла в ноль до резолва, награды должны давать "
                f"${-res_total / max(days, 1e-9):,.0f} на рынок в сутки (рыночных суток {days:,.0f}) — при условии, что "
                "вся награда рынка достаётся нам одним."]
    rated = [g for g in data if g.get("reward_rate")]
    out += ["", f"Награды за ликвидность (текущая настройка CLOB на 06.10.2026, истории наград нет): у {len(rated)} из "
            f"{len(data)} рынков есть ставка; сумма ставок ${sum(g['reward_rate'] for g in rated):,.0f}/сутки; "
            f"max spread {sorted({g['max_spread'] for g in data if g.get('max_spread')})}¢, "
            f"min size {sorted({g['min_size'] for g in data if g.get('min_size')})} долей."]
    return out


def cmd_report(a) -> int:
    parts = []
    for name, fn in (("А", lambda: a_report(a.boot)), ("В", c_report), ("В+", c2_report),
                     ("Б", lambda: b_report(a.boot)), ("Г", lambda: g_report(a.boot)), ("Д", lambda: d_report(a.boot))):
        try:
            parts += fn() + [""]
        except FileNotFoundError:
            parts += [f"### {name}: нет данных", ""]
    text = "\n".join(parts)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("select", "a", "c", "b", "g"):
        sub.add_parser(c)
    dd = sub.add_parser("d")
    dd.add_argument("--n", type=int, default=700)
    cc = sub.add_parser("c2")
    cc.add_argument("--groups", type=int, default=150)
    r = sub.add_parser("report")
    r.add_argument("--out", default="")
    r.add_argument("--boot", type=int, default=SB.BOOT)
    a = ap.parse_args()
    if a.cmd in ("d", "c2"):
        import asyncio

        return asyncio.run(_run_d(a) if a.cmd == "d" else _run_c2(a))
    return {"select": cmd_select, "a": cmd_a, "c": cmd_c, "b": cmd_b, "g": cmd_g, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
