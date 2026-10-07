"""Тест «вероятность направления ETH»: бот (агент Gemini → Opus high, 1 прогноз) против 50% и простой статистики.

    .venv/bin/python tools/eth_direction.py data       # свечи ETH (Binance 1h), ставки финансирования, точки, базовые линии
    .venv/bin/python tools/eth_direction.py gdelt      # заголовки GDELT строго до каждого t (бесплатно)
    .venv/bin/python tools/eth_direction.py forecast [--limit N]
    .venv/bin/python tools/eth_direction.py report [--out файл.md]

Деньги: приложение `forecast-lab`, пользователи `eth:*`, потолок этапа $20; суточный лимит не поднимается.
Данные — ~/.forecast-bot/eth/ (вне репо). Прогнозист — Opus 5.5 high, как в бою: вывод переносим на бота;
по цене проходит (поиск — GDELT, бесплатно; агент Gemini дешёвый): ≈ $0.04 за прогноз × ~196 (оценка).
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "forecast-lab"  # ДО импорта forecast_bot

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import zlib  # noqa: E402
from collections import defaultdict  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from forecast_bot import eth_direction as E, manifold as MF, paths  # noqa: E402

UTC = timezone.utc
T_START = datetime(2026, 7, 1, tzinfo=UTC)
DATA_START = datetime(2025, 6, 1, tzinfo=UTC)   # 365 дней истории для условных базовых линий
STAGE_START = datetime(2026, 10, 7, 21, 30, tzinfo=UTC)
STAGE_CAP_USD = 20.0
PREFIX = "eth"
PER_QUESTION_CALLS = 150


def d() -> Path:
    p = paths.state_dir() / "eth"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


# ─────────────────────────── данные ────────────────────────────────
def prices() -> E.Prices:
    j = json.loads((d() / "ethusdt_1h.json").read_text())
    return E.Prices(j["avail"], j["close"])


def funding() -> list[tuple[float, float]]:
    return [tuple(x) for x in json.loads((d() / "funding.json").read_text())]


def cmd_data(_a) -> int:
    from forecast_bot.polymarket import series_data as SD

    now = datetime.now(UTC)
    bars = SD.binance("ETHUSDT", DATA_START, now)
    (d() / "ethusdt_1h.json").write_text(json.dumps({"avail": bars.avail.tolist(), "close": bars.close.tolist()}))
    fr, start = [], int(DATA_START.timestamp() * 1000)
    while True:
        rows = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                            params={"symbol": "ETHUSDT", "startTime": start, "limit": 1000}, timeout=30).json()
        if not rows:
            break
        fr += [(r["fundingTime"] / 1000, float(r["fundingRate"])) for r in rows]
        start = rows[-1]["fundingTime"] + 1
        time.sleep(0.3)
        if len(rows) < 1000:
            break
    (d() / "funding.json").write_text(json.dumps(fr))
    p, fu = prices(), funding()
    rows = []
    for h, days in E.HORIZONS.items():
        pts = [t for t in E.daily_points(T_START, now) if t + timedelta(days=days) <= now - timedelta(hours=2)]
        pts = [t for t in pts if E.outcome(p, t, h) is not None]
        last_t = max(pts)
        for t in pts:
            x, y_price = p.at(t), p.at(t + timedelta(days=days))
            rows.append({"t": t.isoformat(), "h": h, "x": x, "y_price": y_price, "y": E.outcome(p, t, h),
                         "holdout": E.is_holdout(t, last_t), "p_half": 0.5, "p_freq": E.base_freq(p, t, h),
                         "p_mom": E.conditional(p, t, h, lambda s, h=h: E.momentum_sign(p, s, h),
                                                E.momentum_sign(p, t, h)),
                         "p_fund": E.conditional(p, t, h, lambda s: E.funding_sign(fu, s), E.funding_sign(fu, t))})
    (d() / "points.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"свечей {len(bars.avail)}, ставок финансирования {len(fu)}, точек {len(rows)} "
          f"({', '.join(f'{h}: {sum(1 for r in rows if r['h'] == h)}' for h in E.HORIZONS)}), "
          f"отложено {sum(r['holdout'] for r in rows)}")
    return 0


def points() -> list[dict]:
    return load_jsonl(d() / "points.jsonl")


# ─────────────────────────── gdelt ─────────────────────────────────
QUERY = "Ethereum ETH crypto price"


def cmd_gdelt(a) -> int:
    from concurrent.futures import ThreadPoolExecutor

    from forecast_bot.polymarket import gdelt, gdelt_files as GF

    ts = sorted({datetime.fromisoformat(r["t"]) for r in points()})
    pts = [(t.isoformat(), QUERY, t) for t in ts]
    by_file = GF.plan(pts, a.days, a.per_hour)
    log_p = d() / "gdelt_log.jsonl"
    done = {r["file"] for r in load_jsonl(log_p)}
    todo = sorted(x for x in by_file if x.strftime("%Y%m%d%H%M%S") not in done)
    print(f"точек {len(pts)}, файлов {len(by_file)}, качать {len(todo)}", flush=True)

    def work(f):
        raw = GF.fetch(f)
        c = GF.Collector(keep=200)
        if raw:
            for art in GF.parse_gkg(raw):
                for key, t, words in by_file[f]:
                    c.add(key, art, t, words)
        return f, [{"key": k, "seen": x.seen.isoformat(), "title": x.title, "url": x.url, "domain": x.domain}
                   for k in c.items for x in c.articles(k)]

    with ThreadPoolExecutor(max_workers=4) as pool, log_p.open("a") as log:
        for i, fut in enumerate([pool.submit(work, f) for f in todo], 1):
            try:
                f, hits = fut.result()
            except Exception as exc:
                print(f"ошибка файла: {str(exc)[:80]}", flush=True)
                continue
            log.write(json.dumps({"file": f.strftime("%Y%m%d%H%M%S"), "hits": hits}, ensure_ascii=False) + "\n")
            if i % 300 == 0:
                print(f"{i}/{len(todo)}", flush=True)
    c = GF.Collector(keep=40)
    t_of = {k: t for k, _, t in pts}
    words = GF.words_for(QUERY)
    for rec in load_jsonl(log_p):
        for h in rec["hits"]:
            if h["key"] in t_of:
                c.add(h["key"], gdelt.Article(datetime.fromisoformat(h["seen"]), h["title"], h["url"], h["domain"], ""),
                      t_of[h["key"]], words)
    with (d() / "gdelt.jsonl").open("w") as fh:
        for k, _, t in pts:
            fh.write(json.dumps({"key": k, "t": t.isoformat(), "articles": [
                {"seen": x.seen.isoformat(), "title": x.title, "url": x.url, "domain": x.domain}
                for x in c.articles(k)]}, ensure_ascii=False) + "\n")
    print(f"с новостями {sum(1 for k, _, _ in pts if c.articles(k))} из {len(pts)}")
    return 0


def news() -> dict[str, list]:
    from forecast_bot.polymarket import gdelt

    out = {}
    for r in load_jsonl(d() / "gdelt.jsonl"):
        t = datetime.fromisoformat(r["t"])
        out[r["key"]] = [gdelt.Article(datetime.fromisoformat(x["seen"]), x["title"], x["url"], x["domain"], "")
                         for x in r["articles"] if datetime.fromisoformat(x["seen"]) < t]
    return out


# ─────────────────────────── прогноз ───────────────────────────────
def stage_spent() -> float:
    from forecast_bot import ai_guard

    return ai_guard.app_cost_since(f"forecast-lab:{PREFIX}", STAGE_START.timestamp())


def question_text(r: dict, p: E.Prices) -> tuple[str, str, str]:
    """(вопрос, фон, критерий). Только данные до t: цена в t и дневные закрытия за 14 дней ДО t."""
    t = datetime.fromisoformat(r["t"])
    t_res = t + timedelta(days=E.HORIZONS[r["h"]])
    hist = [(t - timedelta(days=k), p.at(t - timedelta(days=k))) for k in range(14, 0, -1)]
    hist_s = ", ".join(f"{x:%m-%d} {v:,.2f}" for x, v in hist if v is not None)
    q = f"Will the ETH/USDT price on Binance at {t_res:%Y-%m-%d %H:%M} UTC be higher than {r['x']:,.2f} USDT?"
    bg = (f"Now is {t:%Y-%m-%d %H:%M} UTC. ETH/USDT on Binance is {r['x']:,.2f} (1-hour candle closed at this moment). "
          f"Daily closes at 00:00 UTC for the previous 14 days: {hist_s}.")
    crit = (f"Resolves YES if the Binance ETH/USDT 1-hour candle that closes at {t_res:%Y-%m-%d %H:%M} UTC closes above "
            f"{r['x']:,.2f}; otherwise NO.")
    return q, bg, crit


def _setup_guard() -> None:
    from forecast_bot import ai_guard, guarded_llm

    if guarded_llm.APP != "forecast-lab":
        raise RuntimeError(f"guarded_llm.APP={guarded_llm.APP!r}, нужен 'forecast-lab'")
    ai_guard.LIMITS["per_user_day_calls"] = PER_QUESTION_CALLS
    guarded_llm.install_sentinel()
    guarded_llm.start_run(max(0.0, STAGE_CAP_USD - stage_spent()))


async def _forecast(a) -> int:
    from forecasting_tools import BinaryQuestion

    from forecast_bot import ai_guard, guarded_llm
    from forecast_bot.bot import ForecastBot
    from forecast_bot.polymarket.forecaster import _freeze_template_date

    import tools.manifold_backtest as MB

    _setup_guard()
    p, nw = prices(), news()
    out_p = d() / "results.jsonl"
    done = {(r["t"], r["h"]) for r in load_jsonl(out_p)}
    os.environ.update({"FORECAST_RESEARCH": "agent", "FORECAST_SEARCH": "web",
                       "FORECAST_AGENT_MODEL": "openrouter/google/gemini-3.8-flash", "FORECAST_AGENT_MAX_NEWS": "2",
                       "FORECAST_MODEL": "openrouter/anthropic/claude-opus-5.5", "FORECAST_REASONING": "high",
                       "FORECAST_PREDICTIONS": "1"})
    n = 0
    for r in sorted(points(), key=lambda r: (r["t"], r["h"])):
        if (r["t"], r["h"]) in done:
            continue
        if a.limit and n >= a.limit:
            break
        if stage_spent() >= STAGE_CAP_USD:
            print(f"потолок этапа ${STAGE_CAP_USD} — стоп")
            break
        t = datetime.fromisoformat(r["t"])
        os.environ["FORECAST_ASOF"] = t.date().isoformat()
        _freeze_template_date(t)
        MB.install_time_machine(t, nw.get(r["t"], []))
        bot = ForecastBot()
        briefs: list[str] = []
        orig = bot._agent_research

        async def capture(question, _orig=orig):
            text = await _orig(question)
            briefs.append(text)
            return text

        bot._agent_research = capture
        qt, bg, crit = question_text(r, p)
        qid = zlib.crc32(f"{r['t']}|{r['h']}".encode()) % 10**9
        q = BinaryQuestion(question_text=qt, id_of_question=qid, id_of_post=None, page_url="", background_info=bg,
                           resolution_criteria=crit, fine_print="",
                           close_time=t + timedelta(days=E.HORIZONS[r["h"]]))
        user = f"{PREFIX}:{r['t'][:10]}-{r['h']}"
        token = guarded_llm.CURRENT_USER.set(user)
        t0 = time.time()
        try:
            report = await bot.forecast_question(q, return_exceptions=True)
        finally:
            guarded_llm.CURRENT_USER.reset(token)
        cost, calls = ai_guard.spent_by_user(f"forecast-lab:{user}", t0)
        rec = {"t": r["t"], "h": r["h"], "cost_usd": cost, "llm_calls": calls}
        if isinstance(report, BaseException):
            rec["error"] = f"{type(report).__name__}: {str(report)[:200]}"
            if guarded_llm.BUDGET_HITS.pop(user, None):
                print("лимит ai_guard — стоп (суточный потолок forecast-lab?), продолжить в новые сутки")
                with out_p.open("a") as fh:
                    fh.write(json.dumps(rec) + "\n")
                break
        else:
            brief = briefs[0] if briefs else ""
            late = MF.dates_after(brief, t, t + timedelta(days=E.HORIZONS[r["h"]]), known=qt + " " + crit)
            leak, why = await MB.leak_check(f"{PREFIX}:chk-{r['t'][:10]}-{r['h']}", t, qt, brief)
            rec.update(p_bot=float(report.prediction), brief_late_dates=[str(x) for x in late[:5]], brief_leak=leak,
                       brief_leak_why=why)
        with out_p.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n += 1
        print(f"{r['t'][:10]} {r['h']}: бот {rec.get('p_bot', '—')} ${cost:.3f} {rec.get('error', '')[:60]}", flush=True)
    print(f"готово {n}; этап ${stage_spent():.3f} из ${STAGE_CAP_USD}")
    return 0


# ─────────────────────────── отчёт ─────────────────────────────────
def report(boot: int = 2000) -> str:
    from forecast_bot.polymarket import series_backtest as SB

    pts = {(r["t"], r["h"]): r for r in points()}
    res = {(r["t"], r["h"]): r for r in load_jsonl(d() / "results.jsonl") if "p_bot" in r}
    rows = []
    for k, r in pts.items():
        if k not in res:
            continue
        t = datetime.fromisoformat(r["t"])
        iso = t.isocalendar()
        rows.append({**r, "p_bot": res[k]["p_bot"], "cluster": f"{iso[0]}-W{iso[1]:02d}",
                     "clean": not res[k]["brief_leak"] and not res[k]["brief_late_dates"]})
    models = (("бот", "p_bot"), ("50%", "p_half"), ("частота 90 дн", "p_freq"), ("моментум", "p_mom"),
              ("финансирование", "p_fund"))
    br = lambda s, k: sum((x[k] - x["y"]) ** 2 for x in s) / len(s)  # noqa: E731
    lg = lambda s, k: sum(E.log_score(x[k], x["y"]) for x in s) / len(s)  # noqa: E731
    flagged = sum(1 for x in rows if not x["clean"])
    out = [f"Прогнозов {len(rows)}; признаки утечки в справке у {flagged} ({flagged / max(1, len(rows)):.0%}) — "
           "результат с ними и без них.", ""]
    for h in E.HORIZONS:
        for part, sel in (("до отложенной части", lambda x: not x["holdout"]), ("отложенные 30 дней", lambda x: x["holdout"]),
                          ("все, без признаков утечки", lambda x: x["clean"])):
            rs = [x for x in rows if x["h"] == h and sel(x)]
            if not rs:
                continue
            ci = SB.bootstrap(rs, lambda s: br(s, "p_bot") - br(s, "p_half"), boot)
            out += [f"### {h} · {part}: {len(rs)} точек, доля роста {sum(x['y'] for x in rs) / len(rs):.2f}", "",
                    "| прогноз | Brier | log |", "|---|---|---|"] + \
                   [f"| {name} | {br(rs, k):.4f} | {lg(rs, k):.4f} |" for name, k in models] + \
                   ["", f"ΔBrier бот − 50%: {br(rs, 'p_bot') - br(rs, 'p_half'):+.4f}, 90% "
                        f"{'[%+.4f; %+.4f]' % ci if ci else '—'} (кластер — неделя)", ""]
        rs = [x for x in rows if x["h"] == h]
        out += [f"Калибровка бота · {h}:", "", "| корзина | n | средняя p | доля роста |", "|---|---|---|---|"]
        for lo in (0.0, 0.3, 0.4, 0.5, 0.6, 0.7):
            hi = {0.0: 0.3, 0.3: 0.4, 0.4: 0.5, 0.5: 0.6, 0.6: 0.7, 0.7: 1.01}[lo]
            b = [x for x in rs if lo <= x["p_bot"] < hi]
            if b:
                out.append(f"| {lo:.1f}–{min(hi, 1.0):.1f} | {len(b)} | {sum(x['p_bot'] for x in b) / len(b):.3f} | "
                           f"{sum(x['y'] for x in b) / len(b):.3f} |")
        out += ["", f"Бумажная торговля · {h} (вход при p ≥ 0.6 / ≤ 0.4, комиссия 0.1% туда-обратно):", "",
                "| часть | модель | сделок | прибыльных | средний P/L на сделку | сумма P/L | 90% среднего |",
                "|---|---|---|---|---|---|---|"]
        for part, sel in (("до отложенной", lambda x: not x["holdout"]), ("отложенные 30 дн", lambda x: x["holdout"])):
            for name, k in models[:1] + models[2:]:
                sub = [x for x in rs if sel(x)]
                trades = [(x, tr) for x in sub if (tr := E.paper_trade(x[k], x["x"], x["y_price"]))]
                if not trades:
                    out.append(f"| {part} | {name} | 0 | — | — | — | — |")
                    continue
                pnl = [tr.pnl for _, tr in trades]
                ci = SB.bootstrap([dict(x, _p=tr.pnl) for x, tr in trades],
                                  lambda s: sum(z["_p"] for z in s) / len(s), boot)
                out.append(f"| {part} | {name} | {len(pnl)} | {sum(v > 0 for v in pnl) / len(pnl):.0%} | "
                           f"{sum(pnl) / len(pnl):+.2%} | {sum(pnl):+.1%} | "
                           f"{'[%+.2f%%; %+.2f%%]' % (100 * ci[0], 100 * ci[1]) if ci else '—'} |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    from forecast_bot.run import load_env_file

    load_env_file(paths.env_path())
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("data")
    g = sub.add_parser("gdelt")
    g.add_argument("--days", type=int, default=3)
    g.add_argument("--per-hour", type=int, default=1)
    f = sub.add_parser("forecast")
    f.add_argument("--limit", type=int, default=0)
    r = sub.add_parser("report")
    r.add_argument("--out", default="")
    a = ap.parse_args()
    if a.cmd == "data":
        return cmd_data(a)
    if a.cmd == "gdelt":
        return cmd_gdelt(a)
    if a.cmd == "forecast":
        return asyncio.run(_forecast(a))
    text = report()
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
