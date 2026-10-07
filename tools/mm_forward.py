#!/usr/bin/env python3
"""Г, форвард-замер маркетмейкинга на Polymarket (этап 4, решение 07.10.2026). Только чтение, $0, без ключей и ордеров.

    python3 tools/mm_forward.py snap        # один снимок (LaunchAgent каждые 5 мин); после 7 дней — ничего не делает
    python3 tools/mm_forward.py analyze     # итог: награды при наших котировках, неблагоприятный отбор, капитал

Самостоятельный файл: только стандартная библиотека (urllib), без пакета forecast_bot и без venv — LaunchAgent
переживает снос рабочих деревьев. Хосты — только GET: gamma-api / clob / data-api.polymarket.com.
Данные — ~/.forecast-bot/polymarket/mm/ (вне репо). Адреса кошельков из сделок НЕ сохраняются.

Награды (docs.polymarket.com/market-makers/liquidity-rewards, 07.10.2026): S(v,s) = ((v − s)/v)²;
Q_one = Σ S·(бид YES) + Σ S·(аск NO), Q_two = Σ S·(аск YES) + Σ S·(бид NO); Q_min = max(min(Q1,Q2), max(Q1,Q2)/3)
при середине 0.10–0.90, иначе min(Q1,Q2); доля = Q_min наш / Σ Q_min всех. Конкурентов видим только агрегатом стакана:
Σ_i min(a_i, b_i) ≤ min(Σa, Σb), поэтому наша доля здесь — оценка СНИЗУ (консервативно).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
DATA = Path(os.environ.get("MM_DATA_DIR", Path.home() / ".forecast-bot" / "polymarket" / "mm"))
DAYS = 7
MAX_MARKETS = 50
OTHER_SLOTS = 10      # «5–10 похожих» с ненулевой ставкой наград — места не отдаются сериям
TAGS = ("ukraine", "russia", "geopolitics")
CORE = ("target Kyiv on", "target Moscow on")    # серии из ТЗ; плюс похожие рынки с наградами
UA = {"User-Agent": "Mozilla/5.0 forecast-bot research (read-only)"}
HOSTS = {"gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com"}
PAUSE_S = 0.25
SNAP_EVERY_S = 300


def get(url: str, params: dict | None = None, timeout: int = 30):
    """Только GET и только к трём хостам Polymarket."""
    host = urllib.parse.urlparse(url).hostname
    if urllib.parse.urlparse(url).scheme != "https" or host not in HOSTS:
        raise ValueError(f"хост не разрешён: {host}")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        time.sleep(PAUSE_S)
        return json.load(r)


# ─────────────────────────── чистые функции ─────────────────────────
def score(v_cents: float, s_cents: float) -> float:
    """S(v,s) = ((v − s)/v)² при s < v, иначе 0."""
    if v_cents <= 0 or s_cents >= v_cents or s_cents < 0:
        return 0.0
    return ((v_cents - s_cents) / v_cents) ** 2


def mid_of(bids: list, asks: list) -> float | None:
    if not bids or not asks:
        return None
    return (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2


def side_q(levels: list, mid: float, v_cents: float, min_size: float) -> float:
    """Σ S·size по уровням в пределах v от середины (уровни меньше min_size не считаются)."""
    return sum(score(v_cents, abs(p - mid) * 100) * q for p, q in levels if q >= min_size)


def q_min(q1: float, q2: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1, q2) / 3.0)
    return min(q1, q2)


def book_q(yes: dict, no: dict, mid: float, v: float, min_size: float) -> tuple[float, float]:
    """(Q_one, Q_two) стакана: бид YES + аск NO / аск YES + бид NO (NO-уровни по своей середине 1 − mid)."""
    q1 = side_q(yes["bids"], mid, v, min_size) + side_q(no["asks"], 1 - mid, v, min_size)
    q2 = side_q(yes["asks"], mid, v, min_size) + side_q(no["bids"], 1 - mid, v, min_size)
    return q1, q2


def our_share(mid: float, delta: float, size: float, v: float, comp_q: tuple[float, float]) -> float:
    """Доля наград при наших котировках середина ± delta (бид YES и аск YES по `size`)."""
    s = score(v, delta * 100) * size
    ours = q_min(s, s, mid)
    comp = q_min(comp_q[0], comp_q[1], mid)
    return ours / (ours + comp) if ours > 0 else 0.0


def yes_trade(t: dict, yes_token: str) -> tuple[int, str, float, float] | None:
    """Сделка → (время, сторона тейкера по «Да», цена «Да», размер). Кошелёк отбрасывается."""
    try:
        ts, side, p, q = int(t["timestamp"]), str(t["side"]).upper(), float(t["price"]), float(t["size"])
    except (KeyError, TypeError, ValueError):
        return None
    if str(t.get("asset")) != str(yes_token):
        p, side = 1 - p, ("SELL" if side == "BUY" else "BUY")
    return ts, ("buy" if side == "BUY" else "sell"), p, q


def fills(trades: list, mid: float, delta: float, size: float) -> list[dict]:
    """Сделки после снимка, прошедшие через наши котировки (мы первые в очереди — оптимистично)."""
    ask, bid = min(0.99, mid + delta), max(0.01, mid - delta)
    out = []
    for ts, side, p, q in trades:
        if side == "buy" and p >= ask:
            out.append({"ts": ts, "we": "sell", "px": ask, "qty": min(q, size)})
        elif side == "sell" and p <= bid:
            out.append({"ts": ts, "we": "buy", "px": bid, "qty": min(q, size)})
    return out


def collateral(mid: float, delta: float, size: float) -> float:
    """Залог двух котировок: бид YES — цена×доли, аск YES (= бид NO) — (1 − цена)×доли."""
    return size * (max(0.01, mid - delta) + (1 - min(0.99, mid + delta)))


# ─────────────────────────── снимок ─────────────────────────────────
def state_path() -> Path:
    return DATA / "state.json"


def load_state() -> dict:
    p = state_path()
    return json.loads(p.read_text()) if p.exists() else {}


def discover() -> list[dict]:
    """Открытые рынки: серии CORE + похожие геополитические с ненулевой ставкой наград. Не больше MAX_MARKETS."""
    seen, core, other = set(), [], []
    for tag in TAGS:
        for off in (0, 100, 200):
            try:
                evs = get("https://gamma-api.polymarket.com/events",
                          {"tag_slug": tag, "closed": "false", "active": "true", "limit": 100, "offset": off})
            except Exception:
                break
            for e in evs:
                for m in e.get("markets") or []:
                    if m.get("closed") or not m.get("acceptingOrders", True) or m.get("id") in seen:
                        continue
                    seen.add(m.get("id"))
                    rec = {"id": str(m.get("id")), "cid": m.get("conditionId"), "event": str(e.get("id")),
                           "title": e.get("title") or "", "question": m.get("question") or "",
                           "tokens": json.loads(m.get("clobTokenIds") or "[]"), "end": m.get("endDate"),
                           "volume": float(m.get("volumeNum") or 0)}
                    if len(rec["tokens"]) != 2:
                        continue
                    (core if any(c in rec["title"] for c in CORE) else other).append(rec)
            if len(evs) < 100:
                break
    return core, other


def rewards(cid: str) -> dict:
    r = (get(f"https://clob.polymarket.com/markets/{cid}") or {}).get("rewards") or {}
    return {"min_size": float(r.get("min_size") or 0), "max_spread": float(r.get("max_spread") or 0),
            "rate": sum(float(x.get("rewards_daily_rate") or 0) for x in (r.get("rates") or []))}


def book(token: str) -> dict:
    b = get("https://clob.polymarket.com/book", {"token_id": token}) or {}
    lv = lambda xs: sorted(((float(x["price"]), float(x["size"])) for x in xs or []))  # noqa: E731
    return {"bids": lv(b.get("bids"))[-20:], "asks": lv(b.get("asks"))[:20]}


def snap(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    DATA.mkdir(parents=True, exist_ok=True)
    st = load_state()
    st.setdefault("started", now.isoformat())
    if now > datetime.fromisoformat(st["started"]) + timedelta(days=DAYS):
        print("замер окончен (7 дней) — снимков больше нет; LaunchAgent можно выгрузить")
        return 0
    if not st.get("markets") or now.timestamp() - st.get("discovered", 0) > 3600:
        core, other = discover()
        rated = []
        for m in other[:200]:
            try:
                m["rw"] = rewards(m["cid"])
            except Exception:
                continue
            if m["rw"]["rate"] > 0:
                rated.append(m)
        rated.sort(key=lambda m: -m["rw"]["rate"])
        core.sort(key=lambda m: m.get("end") or "")  # ближайшие даты — там торгуют
        st["markets"] = core[:MAX_MARKETS - OTHER_SLOTS] + rated[:OTHER_SLOTS]
        st["discovered"] = now.timestamp()
    last = st.setdefault("last_trade_ts", {})
    out = DATA / f"snap-{now:%Y%m%d}.jsonl"
    n = 0
    with out.open("a") as fh:
        for m in st["markets"]:
            try:
                rw = rewards(m["cid"])
                yes, no = book(m["tokens"][0]), book(m["tokens"][1])
                raw = get("https://data-api.polymarket.com/trades", {"market": m["cid"], "limit": 500})
            except Exception as exc:
                fh.write(json.dumps({"ts": now.isoformat(), "market": m["id"], "error": str(exc)[:120]}) + "\n")
                continue
            since = last.get(m["id"], int(now.timestamp()) - SNAP_EVERY_S)
            trades = sorted(v for t in raw if (v := yes_trade(t, m["tokens"][0])) and v[0] > since)
            if trades:
                last[m["id"]] = trades[-1][0]
            fh.write(json.dumps({"ts": now.isoformat(), "market": m["id"], "event": m["event"], "title": m["title"],
                                 "question": m["question"], "end": m["end"], "core": any(c in m["title"] for c in CORE),
                                 "rewards": rw, "yes": yes, "no": no, "trades": trades}) + "\n")
            n += 1
    state_path().write_text(json.dumps(st))
    print(f"{now:%Y-%m-%d %H:%M} снимок: рынков {n} → {out.name}")
    return 0


# ─────────────────────────── анализ ─────────────────────────────────
def load_snaps() -> list[dict]:
    rows = []
    for p in sorted(DATA.glob("snap-*.jsonl")):
        rows += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return [r for r in rows if "error" not in r]


def analyze(fractions=(0.5, 0.8)) -> str:
    """δ = доля от max spread рынка; размер — min_size наград (минимум 20 долей)."""
    snaps = load_snaps()
    by_m: dict[str, list] = {}
    for r in snaps:
        by_m.setdefault(r["market"], []).append(r)
    outcomes = {}
    for mid_ in by_m:
        try:
            g = get(f"https://gamma-api.polymarket.com/markets/{mid_}")
            pr = json.loads(g.get("outcomePrices") or "[]")
            if g.get("closed") and pr in (["1", "0"], ["0", "1"]):
                outcomes[mid_] = 1 if pr == ["1", "0"] else 0
        except Exception:
            pass
    lines = [f"Снимков {len(snaps)}, рынков {len(by_m)} (из них серий Киев/Москва "
             f"{sum(1 for v in by_m.values() if v[0]['core'])}), закрылись {len(outcomes)}.", "",
             "| δ (доля max spread) | рыночных снимков | награды, $ | награды $/рынок/сутки | заполнений | "
             "марк-аут 5 / 30 / 120 мин, ¢/доля | до резолва, $ (закрытые) | итог, $ | капитал макс., $ |",
             "|---|---|---|---|---|---|---|---|---|"]
    for f in fractions:
        reward = 0.0
        n_snap = 0
        mk = {5: [], 30: [], 120: []}
        res_pnl = 0.0
        all_fills = 0
        coll_by_ts: dict[str, float] = {}
        days = 0.0
        for mkt, rs in by_m.items():
            rs.sort(key=lambda r: r["ts"])
            mids = []
            for r in rs:
                m = mid_of(r["yes"]["bids"], r["yes"]["asks"])
                mids.append((datetime.fromisoformat(r["ts"]).timestamp(), m))
            net = 0.0  # долей «Да»: + куплено, − продано (короткая «Да» = держим «Нет»)
            for i, r in enumerate(rs):
                m = mids[i][1]
                v, size = r["rewards"]["max_spread"], max(20.0, r["rewards"]["min_size"])
                if m is None or v <= 0:
                    continue
                n_snap += 1
                delta = f * v / 100
                comp = book_q(r["yes"], r["no"], m, v, r["rewards"]["min_size"])
                reward += r["rewards"]["rate"] * SNAP_EVERY_S / 86400 * our_share(m, delta, size, v, comp)
                inv = net * m if net > 0 else -net * (1 - m)
                coll_by_ts[r["ts"]] = coll_by_ts.get(r["ts"], 0.0) + collateral(m, delta, size) + inv
                nxt = rs[i + 1]["trades"] if i + 1 < len(rs) else []  # сделки до следующего снимка
                for fl in fills(nxt, m, delta, size):
                    all_fills += 1
                    net += fl["qty"] if fl["we"] == "buy" else -fl["qty"]
                    for n in mk:
                        later = [x for t, x in mids if t >= fl["ts"] + 60 * n and x is not None]
                        if later:
                            sign = 1 if fl["we"] == "sell" else -1
                            mk[n].append(((fl["px"] - later[0]) * sign, fl["qty"]))
                    if mkt in outcomes:
                        y = outcomes[mkt]
                        res_pnl += fl["qty"] * ((fl["px"] - y) if fl["we"] == "sell" else (y - fl["px"]))
            days += SNAP_EVERY_S * len(rs) / 86400
        mks = " / ".join(f"{100 * sum(a * q for a, q in mk[n]) / max(1e-9, sum(q for _, q in mk[n])):+.2f}"
                         for n in mk)
        lines.append(f"| {f:.1f} | {n_snap} | {reward:,.2f} | {reward / max(days, 1e-9):,.2f} | {all_fills} | {mks} | "
                     f"{res_pnl:+,.2f} | {reward + res_pnl:+,.2f} | {max(coll_by_ts.values(), default=0):,.0f} |")
    lines += ["", "Оговорки: доля наград — оценка снизу (конкуренты агрегатом стакана); заполнения — оптимистично "
              "(мы первые в очереди, котировка восстанавливается сразу); выборка снимков — раз в 5 мин, Polymarket "
              "считает раз в минуту. Капитал: залог котировок + инвентарь (максимум по времени)."]
    return "\n".join(lines)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "snap"
    if cmd == "snap":
        return snap()
    if cmd == "analyze":
        print(analyze())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
