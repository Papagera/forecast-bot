"""Этап 4 Polymarket: гипотезы без прогноза (почти без ИИ). Только чтение.

А. Систематические перекосы цены: «длинные выстрелы» (YES 1–10¢) и «фавориты» (YES 90–99¢) по категориям и сроку до
   планового конца. Правило выбирается на обучении (конец рынка в июле–августе), проверяется на сентябре — без подгонки.
В. Логическая несогласованность: взаимоисключающие исходы (negRisk-группа) с суммой цен ≠ 1 и лестницы «к дате»
   с нарушением монотонности P(к d1) ≤ P(к d2).
Б. Скорость (Украина): event study «пост в папке → минутная цена рынка».

Цена — история CLOB (не стакан): исполнимость оценивается полуспредом по сегменту объёма + taker fee (feeType).
Критерий «продолжаем» (ТЗ income 06.10.2026): ROI на проверке > 0, 90%-интервал не захватывает 0, ≥ 30 сделок.
"""
from __future__ import annotations

import re
import zlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from forecast_bot.polymarket import backtest as B
from forecast_bot.polymarket import series_backtest as SB

UTC = timezone.utc
HORIZONS_H = (6, 24, 72, 168)
LONG = (0.01, 0.10)     # [lo, hi)
FAV = (0.90, 0.99)      # (lo, hi]
TRAIN_MONTHS = (7, 8)
TEST_MONTHS = (9,)
MIN_TRADES = 30


def category(fee_type: Optional[str]) -> str:
    t = (fee_type or "").lower()
    return t.split("_", 1)[0] if t else "none"


def sample_rank(market_id: str) -> int:
    """Порядок отбора внутри (день × категория): хэш id — не зависит ни от цены, ни от исхода."""
    return zlib.crc32(str(market_id).encode())


def bucket(p: float) -> Optional[str]:
    if LONG[0] <= p < LONG[1]:
        return "long"
    if FAV[0] < p <= FAV[1]:
        return "fav"
    return None


def period(end: datetime) -> Optional[str]:
    if end.month in TRAIN_MONTHS:
        return "train"
    if end.month in TEST_MONTHS:
        return "test"
    return None


# ─────────────────────────── А: перекосы ───────────────────────────
RULE_SIDE = {"long": "no", "fav": "yes"}  # гипотеза «перекоса фаворит–аутсайдер»: аутсайдеры переоценены


def rule_trade(r: dict, side: str, spread_mult: float = 1.0) -> Optional[B.Trade]:
    """Покупка стороны `side` по цене рынка + полспреда сегмента + taker fee; держим до резолва."""
    seg = B.segment(float(r.get("volume") or 0))
    if seg == "micro":
        return None
    forced = 1.0 if side == "yes" else 0.0  # «прогноз» на краю — сторона сделки задана правилом, порог 0
    return B.paper_trade(forced, r["p_mkt"], r["outcome"], 0.0, B.SPREAD[seg] * spread_mult, r["fee_rate"])


def cell(r: dict) -> tuple:
    return (r["bucket"], r["category"], r["horizon"])


def select_rules(train: list[dict], min_trades: int = MIN_TRADES) -> dict[tuple, dict]:
    """Ячейки (корзина × категория × срок) с ROI > 0 и ≥ min_trades на ОБУЧЕНИИ. Сторона — из гипотезы RULE_SIDE."""
    by = defaultdict(list)
    for r in train:
        if r.get("bucket"):
            by[cell(r)].append(r)
    out = {}
    for c, rs in by.items():
        ts = [t for r in rs if (t := rule_trade(r, RULE_SIDE[c[0]]))]
        if len(ts) >= min_trades and (roi := SB.roi(ts)) is not None and roi > 0:
            out[c] = {"n": len(ts), "roi": roi}
    return out


def apply_rules(rows: list[dict], rules: Iterable[tuple], spread_mult: float = 1.0) -> list[tuple[dict, B.Trade]]:
    rules = set(rules)
    return [(r, t) for r in rows if r.get("bucket") and cell(r) in rules
            if (t := rule_trade(r, RULE_SIDE[r["bucket"]], spread_mult))]


def verdict(pairs: list[tuple[dict, B.Trade]], boot: int = SB.BOOT) -> dict:
    """ROI сделок + 90%-интервал (кластеры — события) + «продолжаем?» по критерию ТЗ."""
    ts = [t for _, t in pairs]
    roi = SB.roi(ts)
    rows = [dict(r, _t=t) for r, t in pairs]
    ci = SB.bootstrap(rows, lambda s: SB.roi([x["_t"] for x in s]), boot) if rows else None
    go = bool(roi is not None and roi > 0 and ci and ci[0] > 0 and len(ts) >= MIN_TRADES)
    return {"trades": len(ts), "events": len({r["cluster"] for r, _ in pairs}), "roi": roi, "ci": ci, "go": go}


# ─────────────────────────── В: несогласованность ───────────────────
def price_at(history: list[tuple[int, float]], ts: float, max_age_s: float = 3 * 3600) -> Optional[float]:
    """Последняя цена не позже ts и не старше max_age_s (застывшая котировка — не цена)."""
    prev = [(t, p) for t, p in history if t <= ts]
    if not prev or ts - prev[-1][0] > max_age_s:
        return None
    return prev[-1][1]


ACTIVE_LOOKBACK_S = 24 * 3600


def active_price(history: list[tuple[int, float]], ts: float, max_age_s: float = 3 * 3600) -> Optional[float]:
    """Цена, только если ряд «живой»: за 24 ч до ts она менялась. Иначе это котировка пустого стакана (у спорта за
    неделю до матча все исходы стоят ~0.50 — сумма 1.4 выглядела бы «арбитражем»)."""
    p = price_at(history, ts, max_age_s)
    if p is None:
        return None
    recent = {round(q, 4) for t, q in history if ts - ACTIVE_LOOKBACK_S <= t <= ts}
    return p if len(recent) >= 2 else None


def leg_cost(p: float, volume: float, fee_rate: float) -> Optional[float]:
    """Издержки одной ноги на долю: полспреда сегмента + taker fee. Микро — неисполнимо."""
    seg = B.segment(volume)
    if seg == "micro":
        return None
    c = min(0.999, max(0.001, p))
    return B.SPREAD[seg] / 2 + fee_rate * c * (1 - c)


@dataclass
class Window:
    kind: str           # negrisk_over | negrisk_under | ladder
    group: str
    ts: int
    edge: float         # прибыль на комплект после издержек, $ на 1 долю каждой ноги
    legs: int
    min_volume: float


def trade_price(history: list[tuple[int, float]], ts: float) -> Optional[float]:
    """Цена последней РЕАЛЬНОЙ сделки не старше 2 ч (история — сделки data-api, а не середина CLOB)."""
    return price_at(history, ts, 2 * 3600)


def negrisk_windows(members: list[dict], hours: Iterable[int], price: Callable = active_price) -> list[Window]:
    """members: [{history, volume, fee_rate, outcome}] — ВСЕ исходы группы (ровно один «Да»).
    Сумма S > 1: купить NO всех → гарантировано n−1, прибыль S − 1 − издержки. S < 1: YES всех → 1 − S − издержки."""
    if len(members) < 2 or sum(m["outcome"] for m in members) != 1:
        return []  # список исходов неполный или не взаимоисключающий — это не арбитраж
    out = []
    for h in hours:
        ps = [price(m["history"], h) for m in members]
        if any(p is None for p in ps):
            continue
        costs = []
        for m, p in zip(members, ps):
            c = leg_cost(p, m["volume"], m["fee_rate"])
            if c is None:
                break
            costs.append(c)
        else:
            s, cost = sum(ps), sum(costs)
            if s - 1 - cost > 0:
                out.append(Window("negrisk_over", members[0]["group"], h, s - 1 - cost, len(ps),
                                  min(m["volume"] for m in members)))
            elif 1 - s - cost > 0:
                out.append(Window("negrisk_under", members[0]["group"], h, 1 - s - cost, len(ps),
                                  min(m["volume"] for m in members)))
    return out


_DATE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})"
                   r"(?:,?\s+(\d{4}))?", re.I)
_MON = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                    "september", "october", "november", "december"], 1)}


def ladder_date(item: str, question: str, year: int) -> Optional[datetime]:
    """Дата ступени «к дате X» из groupItemTitle/вопроса; только для вопросов «by …»."""
    if not re.search(r"\bby\b", question or "", re.I):
        return None
    m = _DATE.search(item or "") or _DATE.search(question or "")
    if not m:
        return None
    return datetime(int(m.group(3) or year), _MON[m.group(1).lower()], int(m.group(2)), tzinfo=UTC)


def ladder_template(question: str) -> str:
    """Вопрос без даты — ступени одной лестницы совпадают по нему."""
    return re.sub(r"\s+", " ", _DATE.sub("", question or "")).strip().lower()


def ladder_windows(steps: list[dict], hours: Iterable[int], price: Callable = active_price) -> list[Window]:
    """steps: [{date, history, volume, fee_rate}] одной лестницы. Нарушение: P(к d1) > P(к d2) при d1 < d2.
    Купить YES(d2) и NO(d1): выплата ≥ 1 в любом исходе, прибыль ≥ p1 − p2 − издержки."""
    steps = sorted(steps, key=lambda s: s["date"])
    out = []
    for h in hours:
        for a in range(len(steps)):
            for b in range(a + 1, len(steps)):
                s1, s2 = steps[a], steps[b]
                if s1["date"] >= s2["date"] or s1.get("tmpl") != s2.get("tmpl"):
                    continue  # не ступени одной лестницы: та же дата (другой порог) или другой вопрос
                p1, p2 = price(s1["history"], h), price(s2["history"], h)
                if p1 is None or p2 is None:
                    continue
                c1, c2 = leg_cost(1 - p1, s1["volume"], s1["fee_rate"]), leg_cost(p2, s2["volume"], s2["fee_rate"])
                if c1 is None or c2 is None:
                    continue
                edge = p1 - p2 - c1 - c2
                if edge > 0:
                    out.append(Window("ladder", s1["group"], h, edge, 2, min(s1["volume"], s2["volume"])))
    return out


def implication_windows(a: dict, b: dict, hours: Iterable[int], group: str) -> list[Window]:
    """«Да(a) ⇒ Да(b)» (связь нашёл ИИ по тексту). Нарушение P(a) > P(b): купить NO(a) и YES(b) — выплата ≥ 1."""
    out = []
    for h in hours:
        pa, pb = active_price(a["history"], h), active_price(b["history"], h)
        if pa is None or pb is None:
            continue
        ca, cb = leg_cost(1 - pa, a["volume"], a["fee_rate"]), leg_cost(pb, b["volume"], b["fee_rate"])
        if ca is None or cb is None:
            continue
        edge = pa - pb - ca - cb
        if edge > 0:
            out.append(Window("implied", group, h, edge, 2, min(a["volume"], b["volume"])))
    return out


def dedupe_windows(ws: list[Window], gap_s: int = 6 * 3600) -> list[Window]:
    """Окно, длящееся часами, — одна возможность: в группе берём первое окно и не считаем следующие gap_s."""
    out, last = [], {}
    for w in sorted(ws, key=lambda w: (w.group, w.kind, w.ts)):
        k = (w.group, w.kind)
        if k in last and w.ts - last[k] < gap_s:
            continue
        last[k] = w.ts
        out.append(w)
    return out


# ─────────────────────────── Б: скорость ───────────────────────────
DELAYS_MIN = (0, 2, 5, 15, 30, 60)


def event_study(post_ts: float, history: list[tuple[int, float]], delays=DELAYS_MIN, max_age_s: float = 600
                ) -> Optional[dict[int, float]]:
    """Цена YES в момент поста (последняя ≤ ts, не старше 10 мин) и через N минут. None — нет свежей цены."""
    out = {}
    for dmin in delays:
        p = price_at(history, post_ts + 60 * dmin, max_age_s)
        if p is None:
            return None
        out[dmin] = p
    return out


def first_per_window(items: list[tuple[str, float]], gap_s: float = 3600) -> list[tuple[str, float]]:
    """(рынок, момент поста): серия постов об одном — одно событие, первый пост в окне gap_s."""
    out, last = [], {}
    for mk, ts in sorted(items, key=lambda x: (x[0], x[1])):
        if mk in last and ts - last[mk] < gap_s:
            continue
        last[mk] = ts
        out.append((mk, ts))
    return out


def speed_trade(path: dict[int, float], outcome: int, delay: int, exit_min: Optional[int], volume: float,
                fee_rate: float) -> Optional[B.Trade]:
    """Покупка YES через `delay` мин после поста (пост сообщает о событии → цена «Да» должна расти).
    exit_min=None — держим до резолва; иначе продаём через exit_min мин по цене − полспреда."""
    seg = B.segment(volume)
    if seg == "micro":
        return None
    sp = B.SPREAD[seg]
    c = min(0.999, path[delay] + sp / 2)
    fee = fee_rate * c * (1 - c)
    if exit_min is None:
        return B.Trade("yes", c, fee, (1.0 if outcome == 1 else 0.0) - c - fee)
    out = max(0.0, path[exit_min] - sp / 2)
    fee_out = fee_rate * out * (1 - out)
    return B.Trade("yes", c, fee + fee_out, out - c - fee - fee_out)


# ─────────────────────────── Г: маркетмейкинг ───────────────────────
MARKOUT_MIN = (5, 30, 120)


def yes_view(trade: dict, yes_token: str) -> Optional[tuple[int, str, float, float]]:
    """Сделка data-api → (время, сторона тейкера по «Да»: buy|sell, цена «Да», размер). Сделка по «Нет» зеркалится."""
    try:
        ts, side, p, q = int(trade["timestamp"]), str(trade["side"]).upper(), float(trade["price"]), float(trade["size"])
    except (KeyError, TypeError, ValueError):
        return None
    if str(trade.get("asset")) != str(yes_token):
        p, side = 1 - p, ("SELL" if side == "BUY" else "BUY")
    return ts, ("buy" if side == "BUY" else "sell"), p, q


def simulate_quotes(trades: list[tuple[int, str, float, float]], mids: list[tuple[int, float]], delta: float,
                    size: float, max_age_s: float = 1800) -> list[dict]:
    """Наши котировки середина ± delta (середина — последняя цена СТРОГО до сделки). Тейкер, купивший «Да» по цене
    ≥ нашего ask, первым бьёт нас (мы внутри спреда) — продаём до `size` долей; продавший ≤ bid — покупаем.
    Оптимистично: котировка восстанавливается сразу, очереди нет (истории стакана Polymarket не отдаёт)."""
    fills = []
    for ts, side, p, q in sorted(trades):
        mid = price_at(mids, ts - 1, max_age_s)
        if mid is None:
            continue
        ask, bid = min(0.99, mid + delta), max(0.01, mid - delta)
        if side == "buy" and p >= ask:
            fills.append({"ts": ts, "we": "sell", "px": ask, "qty": min(q, size), "mid": mid})
        elif side == "sell" and p <= bid:
            fills.append({"ts": ts, "we": "buy", "px": bid, "qty": min(q, size), "mid": mid})
    return fills


def markout(fill: dict, mids: list[tuple[int, float]], minutes: int, max_age_s: float = 3600) -> Optional[float]:
    """Прибыль на долю через N минут по середине: продали — px − mid(t+N); купили — mid(t+N) − px."""
    m = price_at(mids, fill["ts"] + 60 * minutes, max_age_s)
    if m is None:
        return None
    return fill["px"] - m if fill["we"] == "sell" else m - fill["px"]


def to_resolution(fill: dict, outcome: int) -> float:
    return fill["px"] - outcome if fill["we"] == "sell" else outcome - fill["px"]
