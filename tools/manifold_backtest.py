"""Бэктест бота на закрытых рынках Manifold: боевой путь (агент Gemini → Opus high, 5 прогнозов) против толпы на t.

    .venv/bin/python tools/manifold_backtest.py select     # рынки, закрытые 08.07–07.10.2026; вероятность толпы на t
    .venv/bin/python tools/manifold_backtest.py gdelt      # заголовки GDELT строго до t (бесплатно)
    .venv/bin/python tools/manifold_backtest.py check      # ИИ: описание рынка не раскрывает исход (≈ $0.001/рынок)
    .venv/bin/python tools/manifold_backtest.py forecast [--limit N]
    .venv/bin/python tools/manifold_backtest.py report [--out файл.md]

Деньги: приложение `forecast-lab` (не боевое), пользователи `bt:*`, потолок этапа $10; суточный лимит не поднимается.
Данные — ~/.forecast-bot/manifold/ (вне репо).
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "forecast-lab"  # ДО импорта forecast_bot: потолок приложения читается при импорте

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import zlib  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from forecast_bot import manifold as MF, paths  # noqa: E402

UTC = timezone.utc
CLOSE_MIN = datetime(2026, 7, 8, tzinfo=UTC)     # t ≥ 01.07 — после cutoff Opus 5.5 (июнь 2026)
CLOSE_MAX = datetime(2026, 10, 7, tzinfo=UTC)
BASE_MIN, BASE_MAX = datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)  # базовая частота «Да»
N_MARKETS = 50
STAGE_START = datetime(2026, 10, 7, 19, tzinfo=UTC)
STAGE_CAP_USD = 10.0
PREFIX = "bt"
CHECK_MODEL = "openrouter/anthropic/claude-haiku-4.5"
UA = {"User-Agent": "forecast-bot research (read-only)"}
PER_QUESTION_CALLS = 150  # как в бою (forecast.yml): 5 прогнозов не влезают в штатные 20 вызовов/сутки на вопрос


def d() -> Path:
    p = paths.state_dir() / "manifold"
    p.mkdir(parents=True, exist_ok=True)
    return p


def mget(path: str, params: dict | None = None, tries: int = 4):
    """GET к api.manifold.markets с паузой и отступом на 429/5xx."""
    for k in range(tries):
        r = requests.get(MF.API + path, params=params, headers=UA, timeout=40)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(5 * 2 ** k)
            continue
        r.raise_for_status()
        time.sleep(0.3)
        return r.json()
    raise RuntimeError(f"manifold {path}: {r.status_code}")


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def to_rec(m: MF.MfMarket, **extra) -> dict:
    return {"id": m.id, "question": m.question, "description": m.description, "created": m.created.isoformat(),
            "close": m.close.isoformat(), "resolved_at": m.resolved_at.isoformat() if m.resolved_at else None,
            "outcome": m.outcome, "bettors": m.bettors, "volume": m.volume, "groups": m.groups, "url": m.url, **extra}


def from_rec(r: dict) -> MF.MfMarket:
    return MF.MfMarket(id=r["id"], question=r["question"], description=r["description"],
                       created=datetime.fromisoformat(r["created"]), close=datetime.fromisoformat(r["close"]),
                       resolved_at=datetime.fromisoformat(r["resolved_at"]) if r["resolved_at"] else None,
                       outcome=r["outcome"], bettors=r["bettors"], volume=r["volume"], groups=r["groups"], url=r["url"])


# ─────────────────────────── select ────────────────────────────────
CREATED_FLOOR = datetime(2025, 1, 1, tzinfo=UTC)


def crawl_all() -> list[dict]:
    """Все рынки (/v0/markets, от новых по созданию, страницы по 1000 через before=id) до CREATED_FLOOR.
    Поиск /search-markets не листается дальше offset 2000 (400 — живьём 07.10.2026). Кэш — в каталоге данных."""
    cache = d() / "all_markets.jsonl"
    if cache.exists():
        return load_jsonl(cache)
    out, before = [], None
    while True:
        params = {"limit": 1000, **({"before": before} if before else {})}
        rows = mget("/markets", params)
        if not rows:
            break
        out += [r for r in rows if r.get("outcomeType") == "BINARY" and r.get("isResolved")]
        before = rows[-1]["id"]
        if MF.ms_to_dt(rows[-1].get("createdTime")) < CREATED_FLOOR:
            break
        if len(out) and len(out) % 5000 < 1000:
            print(f"просмотрено до {MF.ms_to_dt(rows[-1]['createdTime']):%Y-%m-%d}, решённых бинарных {len(out)}",
                  flush=True)
    cache.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
    return out


def crawl(close_min: datetime, close_max: datetime) -> list[dict]:
    lo, hi = close_min.timestamp() * 1000, close_max.timestamp() * 1000
    return [r for r in crawl_all() if lo <= (r.get("closeTime") or 0) <= hi]


def cmd_select(_a) -> int:
    rows = crawl(CLOSE_MIN, CLOSE_MAX)
    why, pool = Counter(), []
    for raw in rows:
        m = MF.from_api(raw)
        if m is None:
            why["не YES/NO"] += 1
            continue
        r = MF.eligible(m, CLOSE_MIN, CLOSE_MAX)
        if r:
            why[r] += 1
            continue
        pool.append(m)
    print(f"просмотрено {len(rows)}, годных {len(pool)}; отбор: {dict(why)}", flush=True)
    picked, n_try = [], 0
    for m in MF.sample(pool, len(pool), per_group=10_000):  # порядок по хэшу; группы — после деталей
        if len(picked) >= N_MARKETS * 2:
            break
        full = mget(f"/market/{m.id}")
        m.groups = list(full.get("groupSlugs") or [])
        m.description = (full.get("textDescription") or "")[:MF.DESC_LIMIT]
        bets = mget("/bets", {"contractId": m.id, "limit": 1000})
        pa = MF.prob_at(bets, m.t)
        n_try += 1
        if pa is None or pa[1] < MF.MIN_BETS_BEFORE_T:
            continue
        picked.append((m, pa))
    chosen = {x.id for x in MF.sample([m for m, _ in picked], N_MARKETS)}
    out = [to_rec(m, p_crowd=pa[0], bets_before_t=pa[1]) for m, pa in picked if m.id in chosen]
    (d() / "markets.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
    # базовая частота «Да» — рынки того же качества, закрытые в апреле–июне (только метаданные, без ставок)
    base = [m for raw in crawl(BASE_MIN, BASE_MAX) if (m := MF.from_api(raw)) and MF.eligible(m, BASE_MIN, BASE_MAX)
            in (None, "создан позже t − 1 сут", "решён до t")]
    rate = sum(m.outcome for m in base) / len(base) if base else 0.5
    (d() / "base.json").write_text(json.dumps({"n": len(base), "yes_rate": rate}))
    print(f"выбрано {len(out)} (проверено деталей {n_try}), групп {len({r['groups'][0] if r['groups'] else '-' for r in out})};"
          f" базовая частота «Да» {rate:.3f} по {len(base)} рынкам апрель–июнь")
    return 0


def markets() -> list[MF.MfMarket]:
    return [from_rec(r) for r in load_jsonl(d() / "markets.jsonl")]


# ─────────────────────────── gdelt ─────────────────────────────────
def cmd_gdelt(a) -> int:
    from concurrent.futures import ThreadPoolExecutor

    from forecast_bot.polymarket import gdelt, gdelt_files as GF

    points = [(m.id, m.question, m.t) for m in markets()]
    by_file = GF.plan(points, a.days, a.per_hour)
    log_p = d() / "gdelt_log.jsonl"
    done = {r["file"] for r in load_jsonl(log_p)}
    todo = sorted(ts for ts in by_file if ts.strftime("%Y%m%d%H%M%S") not in done)
    print(f"точек {len(points)}, файлов {len(by_file)}, качать {len(todo)}", flush=True)

    def work(ts):
        raw = GF.fetch(ts)
        c = GF.Collector(keep=200)
        if raw:
            for art in GF.parse_gkg(raw):
                for key, t, words in by_file[ts]:
                    c.add(key, art, t, words)
        return ts, [{"key": k, "seen": x.seen.isoformat(), "title": x.title, "url": x.url, "domain": x.domain}
                    for k in c.items for x in c.articles(k)]

    with ThreadPoolExecutor(max_workers=4) as pool, log_p.open("a") as log:
        for i, fut in enumerate([pool.submit(work, ts) for ts in todo], 1):
            try:
                ts, hits = fut.result()
            except Exception as exc:
                print(f"ошибка файла: {str(exc)[:80]}", flush=True)
                continue
            log.write(json.dumps({"file": ts.strftime("%Y%m%d%H%M%S"), "hits": hits}, ensure_ascii=False) + "\n")
            if i % 200 == 0:
                print(f"{i}/{len(todo)}", flush=True)
    c = GF.Collector(keep=40)
    t_of = {k: t for k, _, t in points}
    words = {k: GF.words_for(q) for k, q, _ in points}
    for rec in load_jsonl(log_p):
        for h in rec["hits"]:
            if h["key"] in t_of:
                c.add(h["key"], gdelt.Article(datetime.fromisoformat(h["seen"]), h["title"], h["url"], h["domain"], ""),
                      t_of[h["key"]], words[h["key"]])
    with (d() / "gdelt.jsonl").open("w") as fh:
        for k, _, t in points:
            fh.write(json.dumps({"key": k, "t": t.isoformat(), "articles": [
                {"seen": x.seen.isoformat(), "title": x.title, "url": x.url, "domain": x.domain}
                for x in c.articles(k)]}, ensure_ascii=False) + "\n")
    print(f"с новостями {sum(1 for k, _, _ in points if c.articles(k))} из {len(points)}")
    return 0


def news_for() -> dict[str, list]:
    from forecast_bot.polymarket import gdelt

    out = {}
    for r in load_jsonl(d() / "gdelt.jsonl"):
        t = datetime.fromisoformat(r["t"])
        out[r["key"]] = [gdelt.Article(datetime.fromisoformat(x["seen"]), x["title"], x["url"], x["domain"], "")
                         for x in r["articles"] if datetime.fromisoformat(x["seen"]) < t]
    return out


# ─────────────────────────── деньги и ИИ ───────────────────────────
def stage_spent() -> float:
    from forecast_bot import ai_guard

    return ai_guard.app_cost_since(f"forecast-lab:{PREFIX}", STAGE_START.timestamp())


def _setup_guard() -> None:
    from forecast_bot import ai_guard, guarded_llm

    if guarded_llm.APP != "forecast-lab":
        raise RuntimeError(f"guarded_llm.APP={guarded_llm.APP!r}, нужен 'forecast-lab'")
    ai_guard.LIMITS["per_user_day_calls"] = PER_QUESTION_CALLS
    guarded_llm.install_sentinel()
    guarded_llm.start_run(max(0.0, STAGE_CAP_USD - stage_spent()))


async def leak_check(user: str, t: datetime, question: str, text: str) -> tuple[bool, str]:
    from forecast_bot import guarded_llm

    token = guarded_llm.CURRENT_USER.set(user)
    try:
        resp = await guarded_llm.guarded_completion(
            CHECK_MODEL, [{"role": "user", "content": MF.LEAK_PROMPT.format(t=t.date().isoformat(), q=question,
                                                                           text=text[:3000])}],
            max_tokens=150, temperature=0)
    finally:
        guarded_llm.CURRENT_USER.reset(token)
    raw = resp.choices[0].message.content or ""
    m = re.search(r"\{.*?\}", raw, re.S)
    try:
        j = json.loads(m.group(0)) if m else {}
    except ValueError:
        j = {}
    if "leak" not in j:
        return True, "ответ проверки не разобран — считаем утечкой"  # осторожно: сомнение = вон
    return bool(j["leak"]), str(j.get("why", ""))[:200]


async def _check(_a) -> int:
    _setup_guard()
    path = d() / "desc_check.jsonl"
    done = {r["id"] for r in load_jsonl(path)}
    with path.open("a") as fh:
        for m in markets():
            if m.id in done:
                continue
            if stage_spent() >= STAGE_CAP_USD:
                print("потолок этапа — стоп")
                break
            leak, why = await leak_check(f"{PREFIX}:chk-{m.id}", m.t, m.question, m.description or "(no description)")
            fh.write(json.dumps({"id": m.id, "leak": leak, "why": why}, ensure_ascii=False) + "\n")
    rs = load_jsonl(path)
    print(f"проверено {len(rs)}, описание раскрывает исход/будущее: {sum(r['leak'] for r in rs)}; "
          f"этап ${stage_spent():.3f}")
    return 0


# ─────────────────────────── forecast ──────────────────────────────
def _series_cut(dates: list[str], vals: list[float], t: datetime) -> tuple[list[str], list[float]]:
    keep = [(d_, v) for d_, v in zip(dates, vals) if d_ < t.date().isoformat()]
    return [x for x, _ in keep], [y for _, y in keep]


def install_time_machine(t: datetime, headlines: list) -> None:
    """Инструменты агента «как на дату t»: FRED/Yahoo обрезаны, fetch_url выключен, поиск — заголовки GDELT до t."""
    import csv
    import io

    from forecast_bot import agent as A
    from forecast_bot.bot import ForecastBot

    def fred(series_id: str) -> str:
        sid = re.sub(r"[^A-Za-z0-9_]", "", series_id)[:40]
        try:
            r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid}, timeout=20)
            rows = list(csv.reader(io.StringIO(r.text)))[1:]
            dates, vals = [], []
            for row in rows:
                try:
                    vals.append(float(row[1])); dates.append(row[0])
                except (ValueError, IndexError):
                    continue
        except Exception as exc:
            return f"Ошибка FRED: {type(exc).__name__}"
        dates, vals = _series_cut(dates, vals, t)
        return A._series_stats(dates[-400:], vals[-400:], f"FRED {sid} (до {t.date()})")

    def stock(symbol: str) -> str:
        sym = re.sub(r"[^A-Za-z0-9.^=_-]", "", symbol)[:20].upper()
        try:
            r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                             params={"range": "2y", "interval": "1d"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            res = r.json()["chart"]["result"][0]
        except Exception as exc:
            return f"Ошибка Yahoo для {sym}: {type(exc).__name__}"
        closes = res.get("indicators", {}).get("quote", [{}])[0].get("close") or []
        pairs = [(datetime.fromtimestamp(ts, UTC).date().isoformat(), float(c))
                 for ts, c in zip(res.get("timestamp") or [], closes) if c is not None]
        dates, vals = _series_cut([p[0] for p in pairs], [p[1] for p in pairs], t)
        return A._series_stats(dates[-260:], vals[-260:], f"Yahoo {sym} (до {t.date()})")

    def fetch(url: str) -> str:
        return "Инструмент недоступен в бэктесте: страница показала бы текущее состояние, а не на дату t."

    async def search(self, query: str) -> str:
        words = {w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", query)}
        scored = sorted(headlines, key=lambda a: (-sum(w in a.title.lower() for w in words), -a.seen.timestamp()))
        top = [a for a in scored if sum(w in a.title.lower() for w in words) > 0][:10] or scored[:10]
        if not top:
            return "Web search returned no results."
        return "\n".join(f"{i}. {a.title} — {a.url} ({a.seen:%Y-%m-%d %H:%M} UTC)" for i, a in enumerate(top, 1))

    A.fred_series, A.stock_history, A.fetch_url = fred, stock, fetch
    ForecastBot._web_search = search


async def _forecast(a) -> int:
    from forecasting_tools import BinaryQuestion

    from forecast_bot import ai_guard, guarded_llm
    from forecast_bot.bot import ForecastBot
    from forecast_bot.calib import summarize
    from forecast_bot.polymarket.forecaster import _freeze_template_date

    _setup_guard()
    flags = {r["id"]: r["leak"] for r in load_jsonl(d() / "desc_check.jsonl")}
    news = news_for()
    out_p = d() / "results.jsonl"
    done = {r["id"] for r in load_jsonl(out_p)}
    os.environ.update({"FORECAST_RESEARCH": "agent", "FORECAST_SEARCH": "web",
                       "FORECAST_AGENT_MODEL": "openrouter/google/gemini-3.8-flash", "FORECAST_AGENT_MAX_NEWS": "2",
                       "FORECAST_MODEL": "openrouter/anthropic/claude-opus-5.5", "FORECAST_REASONING": "high",
                       "FORECAST_PREDICTIONS": "5"})  # как боевой forecast.yml (07.10.2026)
    n = 0
    for m in markets():
        if m.id in done or flags.get(m.id, True):
            continue  # описание раскрывает исход (или не проверено) — не прогнозируем
        if a.limit and n >= a.limit:
            break
        if stage_spent() >= STAGE_CAP_USD:
            print(f"потолок этапа ${STAGE_CAP_USD} — стоп")
            break
        os.environ["FORECAST_ASOF"] = m.t.date().isoformat()
        _freeze_template_date(m.t)
        install_time_machine(m.t, news.get(m.id, []))
        bot = ForecastBot()
        briefs: list[str] = []
        orig = bot._agent_research

        async def capture(question, _orig=orig):
            text = await _orig(question)
            briefs.append(text)
            return text

        bot._agent_research = capture
        q = BinaryQuestion(question_text=m.question, id_of_question=zlib.crc32(m.id.encode()) % 10**9, id_of_post=None,
                           page_url=m.url, background_info=m.description, resolution_criteria=m.description,
                           fine_print="", close_time=m.close)
        user = f"{PREFIX}:{m.id}"
        token = guarded_llm.CURRENT_USER.set(user)
        t0 = time.time()
        try:
            report = await bot.forecast_question(q, return_exceptions=True)
        finally:
            guarded_llm.CURRENT_USER.reset(token)
        cost, calls = ai_guard.spent_by_user(f"forecast-lab:{user}", t0)
        rec = {"id": m.id, "t": m.t.isoformat(), "cost_usd": cost, "llm_calls": calls}
        if isinstance(report, BaseException):
            rec["error"] = f"{type(report).__name__}: {str(report)[:200]}"
            if guarded_llm.BUDGET_HITS.pop(user, None):
                print("лимит ai_guard — стоп")
                with out_p.open("a") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                break
        else:
            brief = briefs[0] if briefs else ""
            late = MF.dates_after(brief, m.t, m.close, known=m.question + " " + m.description)
            leak, why = await leak_check(f"{PREFIX}:chk-brief-{m.id}", m.t, m.question, brief)
            sets = bot.prediction_sets.get(q.id_of_question, [])
            rec.update(p_bot=float(report.prediction), brief_late_dates=[str(x) for x in late[:5]],
                       brief_leak=leak, brief_leak_why=why, **summarize(sets),
                       **bot.research_stats.get(q.id_of_question, {}))
            rec.pop("research_dropped", None)
        with out_p.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n += 1
        print(f"{m.id}: бот {rec.get('p_bot', '—')} ${cost:.3f} {rec.get('error', '')[:80]}", flush=True)
    print(f"готово {n}; этап ${stage_spent():.3f} из ${STAGE_CAP_USD}")
    return 0


# ─────────────────────────── report ────────────────────────────────
def report(boot: int = 2000) -> str:
    import math

    import numpy as np

    from forecast_bot.polymarket import series_backtest as SB

    ms = {r["id"]: r for r in load_jsonl(d() / "markets.jsonl")}
    base = json.loads((d() / "base.json").read_text())["yes_rate"]
    res = [r for r in load_jsonl(d() / "results.jsonl") if "p_bot" in r]
    rows = []
    for r in res:
        m = ms[r["id"]]
        rows.append({"cluster": r["id"], "group": (m["groups"] or ["-"])[0], "y": m["outcome"], "bot": r["p_bot"],
                     "crowd": m["p_crowd"], "half": 0.5, "base": base, "spread": r.get("predictions_spread"),
                     "clean": not r["brief_leak"] and not r["brief_late_dates"]})
    clip = lambda p: min(0.99, max(0.01, p))  # noqa: E731
    br = lambda s, k: sum((x[k] - x["y"]) ** 2 for x in s) / len(s)  # noqa: E731
    lg = lambda s, k: sum(math.log(clip(x[k]) if x["y"] else 1 - clip(x[k])) for x in s) / len(s)  # noqa: E731
    out = []
    for label, rs in (("все", rows), ("без признаков утечки в справке", [x for x in rows if x["clean"]])):
        if not rs:
            continue
        ci = SB.bootstrap(rs, lambda s: br(s, "bot") - br(s, "crowd"), boot)
        out += [f"### {label}: {len(rs)} рынков", "",
                "| прогноз | Brier | log |", "|---|---|---|"] + \
               [f"| {k} | {br(rs, k):.3f} | {lg(rs, k):.3f} |" for k in ("bot", "crowd", "half", "base")] + \
               ["", f"ΔBrier бот − толпа: {br(rs, 'bot') - br(rs, 'crowd'):+.3f}, 90% "
                    f"{'[%+.3f; %+.3f]' % ci if ci else '—'}", ""]
    clean = [x for x in rows if x["clean"]] or rows
    out += ["### Калибровка бота (рынки без признаков утечки)", "", "| корзина | n | средняя p | доля «Да» |",
            "|---|---|---|---|"]
    for lo in np.arange(0, 1, 0.2):
        rs = [x for x in clean if lo <= x["bot"] < lo + 0.2 or (lo >= 0.8 and x["bot"] == 1)]
        if rs:
            out.append(f"| {lo:.1f}–{lo + 0.2:.1f} | {len(rs)} | {sum(x['bot'] for x in rs) / len(rs):.3f} | "
                       f"{sum(x['y'] for x in rs) / len(rs):.3f} |")
    mb, fy = sum(x["bot"] for x in clean) / len(clean), sum(x["y"] for x in clean) / len(clean)
    out += ["", f"Средняя p бота {mb:.3f} при доле «Да» {fy:.3f} (толпа {sum(x['crowd'] for x in clean) / len(clean):.3f})"
                f" — «занижает Да»: {'да' if mb < fy - 0.05 else 'нет'}."]
    sp = [x for x in clean if x["spread"] is not None]
    if len(sp) >= 10:
        med = sorted(x["spread"] for x in sp)[len(sp) // 2]
        lo_, hi_ = [x for x in sp if x["spread"] <= med], [x for x in sp if x["spread"] > med]
        if lo_ and hi_:
            out.append(f"Разброс 5 прогнозов: медиана {med:.3f}; Brier при малом разбросе {br(lo_, 'bot'):.3f} "
                       f"(n {len(lo_)}), при большом {br(hi_, 'bot'):.3f} (n {len(hi_)}).")
    grp = defaultdict(list)
    for x in clean:
        grp[x["group"]].append(x)
    out += ["", "| группа | n | Brier бот | Brier толпа |", "|---|---|---|---|"]
    for g, rs in sorted(grp.items(), key=lambda kv: -len(kv[1])):
        out.append(f"| {g} | {len(rs)} | {br(rs, 'bot'):.3f} | {br(rs, 'crowd'):.3f} |")
    return "\n".join(out)


def main() -> int:
    from forecast_bot.run import load_env_file

    load_env_file(paths.env_path())
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select")
    g = sub.add_parser("gdelt")
    g.add_argument("--days", type=int, default=3)
    g.add_argument("--per-hour", type=int, default=2)
    sub.add_parser("check")
    f = sub.add_parser("forecast")
    f.add_argument("--limit", type=int, default=0)
    r = sub.add_parser("report")
    r.add_argument("--out", default="")
    a = ap.parse_args()
    if a.cmd == "select":
        return cmd_select(a)
    if a.cmd == "gdelt":
        return cmd_gdelt(a)
    if a.cmd == "check":
        return asyncio.run(_check(a))
    if a.cmd == "forecast":
        return asyncio.run(_forecast(a))
    text = report()
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
