"""Бэктест A Polymarket (ТЗ этапа 3). Только чтение публичных данных; ничего не торгует.

    .venv/bin/python tools/polymarket_backtest.py select [--end-min 2026-07-03] [--end-max 2026-10-01] [--tail 110 --liquid 45 --mid 0]
    .venv/bin/python tools/polymarket_backtest.py forecast --mode none|gdelt [--points t50,t48] [--limit 20]
    .venv/bin/python tools/polymarket_backtest.py report

Данные — ~/.forecast-bot/polymarket/ (вне репо). ИИ — приложение леджера `polymarket` ($2/сутки) + потолок этапа $20.
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "polymarket"  # ДО импорта forecast_bot: потолок приложения читается при импорте

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from collections import Counter  # noqa: E402
from datetime import datetime  # noqa: E402
from urllib.parse import urlparse  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot import paths  # noqa: E402
from forecast_bot.polymarket import backtest as B, gdelt, markets as M  # noqa: E402


def cmd_select(a) -> int:
    out = B.data_dir()
    out.mkdir(parents=True, exist_ok=True)
    keep: dict[str, list[M.Market]] = {"micro": [], "tail": [], "mid": [], "liquid": []}
    quota = {"micro": 0, "tail": a.tail, "mid": a.mid, "liquid": a.liquid}
    existing = out / a.file
    if existing.exists():  # возобновление: уже отобранные рынки (с историей) не качаем заново
        for line in existing.read_text().splitlines():
            if line.strip():
                m = M.Market.from_json(line)
                keep[B.segment(m.volume)].append(m)
        print("подхвачено из файла: " + ", ".join(f"{k} {len(v)}" for k, v in keep.items()), flush=True)
    seen = 0
    try:
        _select_loop(a, keep, quota, out)
    finally:
        path = out / a.file
        path.write_text("\n".join(m.to_json() for v in keep.values() for m in v) + "\n")
        print("отобрано: " + ", ".join(f"{k} {len(v)}" for k, v in keep.items()) + f" → {path}")
    vols = sorted(m.volume for v in keep.values() for m in v)
    if vols:
        q = lambda p: vols[int(p * (len(vols) - 1))]  # noqa: E731
        print(f"объём: p10 {q(.1):,.0f}  p50 {q(.5):,.0f}  p90 {q(.9):,.0f}")
    return 0


def _select_loop(a, keep, quota, out) -> None:
    seen = 0
    for raw in M.iter_closed(a.end_min, a.end_max):
        seen += 1
        m = M.from_gamma(raw)
        if m is None or any(m.id == x.id for v in keep.values() for x in v):
            continue
        seg = B.segment(m.volume)
        if len(keep[seg]) >= quota[seg]:
            if all(len(keep[k]) >= quota[k] for k in keep):
                break
            continue
        try:
            m.history = M.load_history(m)
        except Exception as exc:
            print(f"история {m.id}: {type(exc).__name__}")
            continue
        if len(m.history) < 3:
            continue
        keep[seg].append(m)
        if seen % 500 == 0:
            print(f"просмотрено {seen}: " + ", ".join(f"{k} {len(v)}" for k, v in keep.items()), flush=True)


async def _forecast(a) -> int:
    from forecast_bot import ai_guard, guarded_llm
    from forecast_bot.polymarket import forecaster

    markets = load_markets(a.files)
    cache = load_gdelt_cache() if a.mode == "gdelt" else {}
    res_path = B.data_dir() / "backtest.jsonl"
    done = {(r["market"], r["point"], r["mode"]) for r in B.load_results(res_path)}
    pts = a.points.split(",")
    segs = set(a.segments.split(","))
    n, stats = 0, Counter()
    for m in markets:
        if B.segment(m.volume) not in segs:
            continue
        for point, t in B.points(m.start, m.closed).items():
            if point not in pts or (m.id, point, a.mode) in done:
                continue
            if a.limit and n >= a.limit:
                break
            p_mkt = m.price_before(t.timestamp())
            if p_mkt is None:
                stats["нет цены до t"] += 1
                continue
            if B.stage_budget_left() <= 0:
                print(f"потолок этапа ${B.STAGE_CAP_USD} исчерпан — стоп")
                return 0
            research = ""
            if a.mode == "gdelt":
                # только из кэша (`gdelt-fetch`): прогноз «с поиском» без поиска смешал бы режимы — такой пропускаем
                key = gdelt_key(m.id, point)
                if key not in cache:
                    stats["нет GDELT в кэше"] += 1
                    continue
                research = gdelt.as_research(cache[key])
            user = f"pm{m.id}-{point}-{a.mode}"
            token = guarded_llm.CURRENT_USER.set(user)
            t_start = time.time()
            try:
                p_bot, err = await forecaster.forecast(m, point, t, research)
            finally:
                guarded_llm.CURRENT_USER.reset(token)
            cost, calls = ai_guard.spent_by_user(f"{B.APP}:{user}", t_start)
            n += 1
            if p_bot is None:
                stats["ошибка прогноза"] += 1
                print(f"{m.id} {point}: {err[:160]}")
                if guarded_llm.BUDGET_HITS.pop(user, None):
                    print("лимит ai_guard — стоп")
                    return 0
                continue
            rec = {"market": m.id, "question": m.question, "url": m.url, "segment": B.segment(m.volume),
                   "volume": m.volume, "point": point, "t": t.isoformat(), "mode": a.mode, "p_mkt": p_mkt,
                   "p_bot": p_bot, "outcome": m.outcome, "fee_rate": m.fee_rate, "neg_risk": m.neg_risk,
                   "pre_cutoff": t < B.CUTOFF, "cost_usd": cost, "llm_calls": calls}
            with res_path.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["ok"] += 1
            print(f"{m.id} {point} {a.mode}: бот {p_bot:.2f} рынок {p_mkt:.2f} итог {m.outcome} ${cost:.4f}")
    print("итог:", dict(stats), f"потрачено на этап ${B.stage_spent():.4f}")
    return 0


def load_markets(files: str) -> list:
    out = []
    for name in files.split(","):
        path = B.data_dir() / name
        if path.exists():
            out += [M.Market.from_json(l) for l in path.read_text().splitlines() if l.strip()]
    return out


def gdelt_key(market_id: str, point: str) -> str:
    return f"{market_id}|{point}"


def load_gdelt_cache() -> dict:
    path = B.data_dir() / "gdelt.jsonl"
    cache = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                cache[r["key"]] = [gdelt.Article(datetime.fromisoformat(x["seen"]), x["title"], x["url"], x["domain"],
                                                 x["language"]) for x in r["articles"]]
    return cache


def cmd_gdelt_fetch(a) -> int:
    """Медленно скачать заголовки GDELT до каждой точки и сложить в кэш. Отказ по темпу — длинная пауза, не долбёжка."""
    from forecast_bot.polymarket import http

    path = B.data_dir() / "gdelt.jsonl"
    cache = load_gdelt_cache()
    pts, segs = a.points.split(","), set(a.segments.split(","))
    todo = [(m, p, t) for m in load_markets(a.files) if B.segment(m.volume) in segs
            for p, t in B.points(m.start, m.closed).items() if p in pts and gdelt_key(m.id, p) not in cache]
    print(f"в кэше {len(cache)}, качать {len(todo)}", flush=True)
    http.MIN_INTERVAL_S[urlparse(gdelt.URL).hostname] = a.pace
    fails = 0
    for i, (m, p, t) in enumerate(todo, 1):
        try:
            arts = gdelt.search(m.question, t, retries=1)
        except RuntimeError as exc:
            if "rate limited" not in str(exc):
                print(f"{i}/{len(todo)} {m.id} {p}: {str(exc)[:90]} — пропуск (не темп)", flush=True)
                continue
            fails += 1
            print(f"{i}/{len(todo)} {m.id} {p}: {str(exc)[:90]} — пауза {a.cooldown} с (подряд {fails})", flush=True)
            if fails >= a.max_fails:
                print("GDELT не отвечает — стоп, кэш сохранён")
                return 0
            time.sleep(a.cooldown)
            continue
        fails = 0
        rec = {"key": gdelt_key(m.id, p), "t": t.isoformat(),
               "articles": [{"seen": x.seen.isoformat(), "title": x.title, "url": x.url, "domain": x.domain,
                             "language": x.language} for x in arts]}
        with path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if i % 10 == 0:
            print(f"{i}/{len(todo)}: последний {len(arts)} статей", flush=True)
    print("готово")
    return 0


def cmd_report(_a) -> int:
    rows = B.load_results(B.data_dir() / "backtest.jsonl")
    print(f"строк: {len(rows)}; потрачено на этап ${B.stage_spent():.4f} из ${B.STAGE_CAP_USD}\n")
    print(B.report(rows))
    return 0


def main() -> int:
    from forecast_bot.run import load_env_file

    load_env_file(paths.env_path())
    os.environ.setdefault("FORECAST_RESEARCH", "none")
    os.environ.setdefault("FORECAST_MODEL", "openrouter/anthropic/claude-opus-5.5")
    os.environ.setdefault("FORECAST_REASONING", "high")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--end-min", default="2026-07-03T00:00:00Z")  # после cutoff Opus 5.5 (июнь 2026)
    s.add_argument("--end-max", default="2026-10-01T00:00:00Z")
    s.add_argument("--tail", type=int, default=110)
    s.add_argument("--liquid", type=int, default=45)
    s.add_argument("--mid", type=int, default=0)
    s.add_argument("--file", default="markets.jsonl")  # markets_pre.jsonl — контроль «до cutoff»
    g = sub.add_parser("gdelt-fetch")
    g.add_argument("--points", default="t50,t48")
    g.add_argument("--segments", default="tail,liquid,mid")
    g.add_argument("--files", default="markets.jsonl,markets_pre.jsonl")
    g.add_argument("--pace", type=float, default=15.0)
    g.add_argument("--cooldown", type=int, default=900)
    g.add_argument("--max-fails", type=int, default=12)
    f = sub.add_parser("forecast")
    f.add_argument("--mode", choices=["none", "gdelt"], required=True)
    f.add_argument("--points", default="t50,t48")
    f.add_argument("--segments", default="tail,liquid,mid,micro")
    f.add_argument("--limit", type=int, default=0)
    f.add_argument("--files", default="markets.jsonl,markets_pre.jsonl")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "select":
        return cmd_select(a)
    if a.cmd == "gdelt-fetch":
        return cmd_gdelt_fetch(a)
    if a.cmd == "forecast":
        from forecast_bot import ai_guard, guarded_llm

        # Только на бэктест (income 05.10.2026): суточный потолок приложения $8 вместо $2; потолок этапа — $20 по-прежнему.
        ai_guard.APP_LIMITS[B.APP] = {"day_usd": B.BACKTEST_DAY_USD}
        guarded_llm.start_run(max(0.0, B.stage_budget_left()))
        return asyncio.run(_forecast(a))
    return cmd_report(a)


if __name__ == "__main__":
    sys.exit(main())
