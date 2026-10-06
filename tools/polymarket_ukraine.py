"""Бэктест №3 Polymarket «Украина»: без новостей / GDELT / Telegram-папка Никиты против цены рынка. Только чтение.

    .venv/bin/python tools/polymarket_ukraine.py select               # рынки (срок ≤ 60 дн) + история цены
    .venv/bin/python tools/polymarket_ukraine.py tg-fetch [--folder 55] # посты каналов папки → локально
    .venv/bin/python tools/polymarket_ukraine.py keywords             # основы слов uk/ru/en на событие (дёшево)
    .venv/bin/python tools/polymarket_ukraine.py gdelt                # заголовки GDELT до t (бесплатно)
    .venv/bin/python tools/polymarket_ukraine.py forecast [--modes none,gdelt,tg] [--points t48,t50] [--limit N]
    .venv/bin/python tools/polymarket_ukraine.py report [--out файл.md]

Данные — ~/.forecast-bot/polymarket/ua/ и …/tg/ (вне репо; тексты постов только там). ИИ — приложение
`polymarket`, пользователи `pm3:*`, потолок этапа $15 (решение Никиты 06.10.2026).
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "polymarket"  # ДО импорта forecast_bot: потолок приложения читается при импорте

import argparse  # noqa: E402
import asyncio  # noqa: E402
import dataclasses  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot import paths  # noqa: E402
from forecast_bot.polymarket import backtest as B, gdelt, markets as M, series as S  # noqa: E402
from forecast_bot.polymarket import series_backtest as SB, tg_news as T  # noqa: E402

UTC = timezone.utc
END_MIN = datetime(2026, 7, 3, tzinfo=UTC)
END_MAX = datetime(2026, 10, 6, tzinfo=UTC)
MAX_LIFE_DAYS = 60
TAGS = {"ukraine": 10, "russia": 10, "geopolitics": 10, "putin": 10, "zelensky": 10}
FOLDER_ID = 55
STAGE3_START = datetime(2026, 10, 6, 16, tzinfo=UTC)
STAGE3_CAP_USD = 15.0
# Никита 06.10.2026: «закончи сегодня». В этот день $8 приложения уже ушли на 3A/№2, поэтому суточный потолок на
# время прогона №3 = $8 + потолок этапа $15 с запасом — связывает именно потолок этапа (pm3:*). Лимит машины $50 — в силе.
STAGE3_DAY_USD = 24.0
LEDGER_PREFIX = "pm3"
KW_MODEL = "openrouter/anthropic/claude-haiku-4.5"
MODES = ("none", "gdelt", "tg")

# Тема: Украина / Россия / война, перемирие, переговоры, фронт, санкции. Выборы в Думу — не про войну.
UA_RU = re.compile(r"ukrain|zelensk|kyiv|kiev|donbas|donetsk|luhansk|zaporizh|kherson|kharkiv|crimea|odesa|"
                   r"russia|moscow|putin|kremlin", re.I)
NOT_WAR = re.compile(r"election|duma|seats|parliament", re.I)


def d() -> Path:
    p = B.data_dir() / "ua"
    p.mkdir(parents=True, exist_ok=True)
    return p


def stage_spent() -> float:
    from forecast_bot import ai_guard

    return ai_guard.app_cost_since(f"{B.APP}:{LEDGER_PREFIX}", STAGE3_START.timestamp())


def stage_budget_left() -> float:
    return STAGE3_CAP_USD - stage_spent()


def on_topic(title: str) -> bool:
    return bool(UA_RU.search(title or "")) and not NOT_WAR.search(title or "")


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def load_markets() -> list[S.SeriesMarket]:
    p = d() / "markets.jsonl"
    return [S.SeriesMarket.from_json(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def market_from(raw: dict, ev: dict):
    base = M.from_gamma(raw, max_life_days=MAX_LIFE_DAYS)
    end = M.parse_ts(raw.get("endDate"))
    if base is None or end is None:
        return None
    life = (end - base.start).total_seconds() / 86400
    if life > MAX_LIFE_DAYS or life < M.MIN_LIFE_DAYS:
        return None
    sm = S.SeriesMarket(**{k: getattr(base, k) for k in base.__dataclass_fields__})
    sm.event_id, sm.event_title, sm.cls, sm.end_planned = str(ev.get("id")), ev.get("title") or "", "ua", end
    sm.rule = (raw.get("description") or "")[:1200]
    return sm


def all_points(ms: list[S.SeriesMarket]) -> list[tuple[S.SeriesMarket, str, datetime, datetime, float]]:
    """(рынок, точка, t, момент цены рынка, цена) — только после cutoff и с ценой до t."""
    out = []
    for m in ms:
        for point, t in S.points(m).items():
            if t < B.CUTOFF:
                continue
            px = S.price_point(m, t)
            if px:
                out.append((m, point, t, px[0], px[1]))
    return out


# ─────────────────────────── select ────────────────────────────────
def cmd_select(_a) -> int:
    seen, fresh = set(), []
    have = {m.id for m in load_markets()}
    for tag, win in TAGS.items():
        for ev in S.iter_events(tag, END_MIN, END_MAX, win):
            if ev.get("id") in seen or not on_topic(ev.get("title") or ""):
                continue
            seen.add(ev.get("id"))
            for raw in ev.get("markets") or []:
                m = market_from(raw, ev)
                if m and m.id not in have and all(m.id != x.id for x in fresh):
                    fresh.append(m)
    print(f"событий по теме {len(seen)}, новых рынков {len(fresh)}", flush=True)
    with (d() / "markets.jsonl").open("a") as fh:
        for m in fresh:
            try:
                m.history = M.load_history(m)
            except Exception as exc:
                print(f"история {m.id}: {type(exc).__name__}", flush=True)
                continue
            if len(m.history) >= 3:
                fh.write(m.to_json() + "\n")
    ms = load_markets()
    pts = all_points(ms)
    print(f"рынков {len(ms)}, событий {len({m.event_id for m in ms})}, точек после cutoff {len(pts)} "
          f"(t48 {sum(1 for p in pts if p[1] == 't48')}), событий с точками {len({p[0].event_id for p in pts})}")
    return 0


# ─────────────────────────── telegram ──────────────────────────────
async def _tg_fetch(a) -> int:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    pts = all_points(load_markets())
    if not pts:
        print("нет точек — сначала select")
        return 1
    since = min(p[3] for p in pts) - timedelta(days=T.DAYS)
    until = max(p[3] for p in pts)
    api_id, api_hash = T.credentials()
    client = TelegramClient(StringSession(T.session_string()), api_id, api_hash)  # строка не сохраняется обратно
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise T.TgSetupError("сессия не авторизована")
        posts = await T.fetch(client, a.folder, since, until)
    finally:
        await client.disconnect()
    out = T.tg_dir()
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    path = out / "posts.jsonl"
    with path.open("w") as fh:
        for p in posts:
            fh.write(p.to_json() + "\n")
    os.chmod(path, 0o600)
    chans = sorted({p.channel for p in posts})
    (out / "channels.json").write_text(json.dumps({str(c): i for i, c in enumerate(chans, 1)}))
    os.chmod(out / "channels.json", 0o600)
    print(f"каналов {len(chans)}, постов {len(posts)} за {since:%Y-%m-%d}…{until:%Y-%m-%d} → {path}")
    return 0


# ─────────────────────────── keywords ──────────────────────────────
def kw_prompt(title: str, questions: list[str]) -> list[dict]:
    return [{"role": "user", "content": (
        "Prediction market event: " + title + "\nMarkets: " + "; ".join(questions[:5]) + "\n\n"
        "Give search keywords to find Telegram posts relevant to this event in Ukrainian and Russian (and English). "
        "Use word STEMS that match inflected forms (e.g. 'київ', 'києв', 'киев', 'kyiv'; 'москв', 'moscow'). "
        "Names of places, people, organisations, weapons, actions. 8-20 items. Do not guess the outcome.\n"
        'Return JSON only: {"keywords": ["...", "..."]}')}]


async def _keywords(_a) -> int:
    from forecast_bot import guarded_llm

    from forecast_bot import ai_guard

    guarded_llm.install_sentinel()
    _check_app()
    ai_guard.APP_LIMITS[B.APP] = {"day_usd": STAGE3_DAY_USD}
    guarded_llm.start_run(max(0.0, stage_budget_left()))
    path = d() / "keywords.json"
    kw = json.loads(path.read_text()) if path.exists() else {}
    by_ev = defaultdict(list)
    for m in load_markets():
        by_ev[m.event_id].append(m)
    for eid, ms in by_ev.items():
        if eid in kw:
            continue
        if stage_budget_left() <= 0:
            print("потолок этапа — стоп")
            break
        token = guarded_llm.CURRENT_USER.set(f"{LEDGER_PREFIX}:kw-ev{eid}")
        try:
            resp = await guarded_llm.guarded_completion(KW_MODEL, kw_prompt(ms[0].event_title, [m.question for m in ms]),
                                                        max_tokens=400, temperature=0)
        finally:
            guarded_llm.CURRENT_USER.reset(token)
        text = resp.choices[0].message.content or ""
        found = re.findall(r"\{.*\}", text, re.S)
        try:
            words = [str(w).strip() for w in json.loads(found[-1])["keywords"] if str(w).strip()]
        except (IndexError, ValueError, KeyError, TypeError):
            print(f"ev{eid}: ответ без JSON — пропуск")
            continue
        kw[eid] = words[:20]
        path.write_text(json.dumps(kw, ensure_ascii=False, indent=1))
        print(f"ev{eid}: {len(kw[eid])} ключей", flush=True)
    return 0


# ─────────────────────────── gdelt ─────────────────────────────────
def cmd_gdelt(a) -> int:
    from concurrent.futures import ThreadPoolExecutor

    from forecast_bot.polymarket import gdelt_files as GF

    points = [(f"{m.id}|{p}", m.question, t_info) for m, p, t, t_info, _ in all_points(load_markets())]
    by_file = GF.plan(points, a.days, a.per_hour)
    log_path = d() / "gdelt_files_log.jsonl"
    done = {r["file"] for r in load_jsonl(log_path)}
    todo = sorted(ts for ts in by_file if ts.strftime("%Y%m%d%H%M%S") not in done)
    print(f"точек {len(points)}, файлов {len(by_file)}, качать {len(todo)}", flush=True)

    def work(ts):
        raw = GF.fetch(ts)
        hits = []
        if raw:
            c = GF.Collector(keep=200)
            for art in GF.parse_gkg(raw):
                for key, t, words in by_file[ts]:
                    c.add(key, art, t, words)
            hits = [{"key": k, "seen": x.seen.isoformat(), "title": x.title, "url": x.url, "domain": x.domain}
                    for k in c.items for x in c.articles(k)]
        return ts, raw is not None, hits

    with ThreadPoolExecutor(max_workers=a.workers) as pool, log_path.open("a") as log:
        for i, fut in enumerate([pool.submit(work, ts) for ts in todo], 1):
            try:
                ts, ok, hits = fut.result()
            except Exception as exc:
                print(f"ошибка файла: {str(exc)[:100]}", flush=True)
                continue
            log.write(json.dumps({"file": ts.strftime("%Y%m%d%H%M%S"), "ok": ok, "hits": hits}, ensure_ascii=False)
                      + "\n")
            log.flush()
            if i % 200 == 0:
                print(f"{i}/{len(todo)}", flush=True)
    c = GF.Collector(keep=15)
    t_of = {k: t for k, _, t in points}
    words_of = {k: GF.words_for(q) for k, q, _ in points}
    for rec in load_jsonl(log_path):
        for h in rec["hits"]:
            if h["key"] in t_of:
                c.add(h["key"], gdelt.Article(datetime.fromisoformat(h["seen"]), h["title"], h["url"], h["domain"], ""),
                      t_of[h["key"]], words_of[h["key"]])
    with (d() / "gdelt.jsonl").open("w") as fh:
        for k, _, t in points:
            fh.write(json.dumps({"key": k, "t": t.isoformat(), "articles": [
                {"seen": x.seen.isoformat(), "title": x.title, "url": x.url, "domain": x.domain}
                for x in c.articles(k)]}, ensure_ascii=False) + "\n")
    print(f"кэш: {len(points)} точек, с новостями {sum(1 for k, _, _ in points if c.articles(k))}")
    return 0


def load_gdelt() -> dict[str, list]:
    out = {}
    for r in load_jsonl(d() / "gdelt.jsonl"):
        t = datetime.fromisoformat(r["t"])
        out[r["key"]] = [gdelt.Article(datetime.fromisoformat(x["seen"]), x["title"], x["url"], x["domain"], "")
                         for x in r["articles"] if datetime.fromisoformat(x["seen"]) < t]
    return out


# ─────────────────────────── forecast ──────────────────────────────
def _check_app() -> None:
    from forecast_bot import guarded_llm

    if guarded_llm.APP != B.APP:
        raise RuntimeError(f"guarded_llm.APP={guarded_llm.APP!r}, нужен {B.APP!r}: FORECAST_APP задан поздно")


def research_for(mode: str, m, point: str, t_info: datetime, gd: dict, posts: list, kw: dict, chans: dict):
    """Текст исследования или None (нет данных для режима — точку пропускаем, а не идём «без новостей»)."""
    if mode == "none":
        return ""
    if mode == "gdelt":
        key = f"{m.id}|{point}"
        if key not in gd:
            return None
        return gdelt.as_research([a for a in gd[key] if a.seen < t_info], limit=15)
    if m.event_id not in kw:
        return None
    return T.as_research(T.select(posts, t_info, kw[m.event_id]), chans)


async def run_forecast(a) -> int:
    from forecast_bot import ai_guard, guarded_llm
    from forecast_bot.polymarket import forecaster

    _check_app()
    ai_guard.APP_LIMITS[B.APP] = {"day_usd": STAGE3_DAY_USD}  # только на прогон (как 3A/№2)
    guarded_llm.start_run(max(0.0, stage_budget_left()))
    modes, pts_order = a.modes.split(","), a.points.split(",")
    res_path = d() / "results.jsonl"
    done = {(r["market"], r["point"], r["mode"]) for r in load_jsonl(res_path)}
    gd = load_gdelt()
    posts = T.load_posts()
    kw = json.loads((d() / "keywords.json").read_text()) if (d() / "keywords.json").exists() else {}
    chans_p = T.tg_dir() / "channels.json"
    chans = {int(k): v for k, v in json.loads(chans_p.read_text()).items()} if chans_p.exists() else {}
    items = sorted(all_points(load_markets()), key=lambda x: (pts_order.index(x[1]) if x[1] in pts_order else 99,
                                                               x[2]))
    n, stats = 0, Counter()
    for m, point, t, t_info, p_mkt in items:
        if point not in pts_order:
            continue
        for mode in modes:  # режимы рядом: частичный прогон сравним между режимами
            if (m.id, point, mode) in done:
                continue
            if a.limit and n >= a.limit:
                print("итог:", dict(stats), f"потрачено на этап №3 ${stage_spent():.4f} из ${STAGE3_CAP_USD}")
                return 0
            research = research_for(mode, m, point, t_info, gd, posts, kw, chans)
            if research is None:
                stats[f"нет данных: {mode}"] += 1
                continue
            if stage_budget_left() <= 0:
                print(f"потолок этапа ${STAGE3_CAP_USD} исчерпан — стоп")
                print("итог:", dict(stats))
                return 0
            user = f"{LEDGER_PREFIX}:{m.id}-{point}-{mode}"
            token = guarded_llm.CURRENT_USER.set(user)
            t0 = time.time()
            try:
                # плановый конец вместо фактического закрытия — в вопрос не попадает момент резолва
                p_bot, err = await forecaster.forecast(dataclasses.replace(m, closed=m.end_planned), point, t_info,
                                                       research)
            finally:
                guarded_llm.CURRENT_USER.reset(token)
            cost, calls = ai_guard.spent_by_user(f"{B.APP}:{user}", t0)
            n += 1
            if p_bot is None:
                stats["ошибка"] += 1
                print(f"{m.id} {point} {mode}: {err[:150]}", flush=True)
                if guarded_llm.BUDGET_HITS.pop(user, None):
                    print("лимит ai_guard — стоп")
                    print("итог:", dict(stats))
                    return 0
                continue
            rec = {"market": m.id, "event": m.event_id, "cluster": f"ev{m.event_id}", "question": m.question,
                   "family": "Украина", "segment": B.segment(m.volume), "volume": m.volume, "point": point,
                   "t": t.isoformat(), "t_info": t_info.isoformat(), "mode": mode, "p_mkt": p_mkt, "p_bot": p_bot,
                   "outcome": m.outcome, "fee_rate": m.fee_rate, "cost_usd": cost, "llm_calls": calls,
                   "research_items": research.count("\n[") if research else 0}
            with res_path.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["ok"] += 1
            print(f"{m.id} {point} {mode}: бот {p_bot:.2f} рынок {p_mkt:.2f} итог {m.outcome} ${cost:.4f}", flush=True)
    print("итог:", dict(stats), f"потрачено на этап №3 ${stage_spent():.4f} из ${STAGE3_CAP_USD}")
    return 0


# ─────────────────────────── report ────────────────────────────────
def report(rows: list[dict], boot: int = SB.BOOT) -> str:
    """По режимам на ОБЩИХ точках (рынок × точка, где есть все режимы) — иначе режимы сравниваются на разных рынках."""
    by = defaultdict(dict)
    for r in rows:
        by[(r["market"], r["point"])][r["mode"]] = r
    modes = sorted({r["mode"] for r in rows}, key=lambda x: MODES.index(x) if x in MODES else 9)
    common = [v for v in by.values() if all(mo in v for mo in modes)]
    out = [f"Общих точек (все режимы): {len(common)}, событий {len({next(iter(v.values()))['cluster'] for v in common})}",
           "", "| точка | режим | n | событий | Brier бот | Brier рынок | log бот | log рынок | ΔBrier бот−рынок, 90% |",
           "|---|---|---|---|---|---|---|---|---|"]
    for point in ("t48", "t50", "все"):
        for mo in modes:
            rs = [v[mo] for v in common if point == "все" or v[mo]["point"] == point]
            rs = [dict(r, p_quant=r["p_bot"]) for r in rs]
            if not rs:
                continue
            br = lambda k, s=rs: sum(B.scores(r[k], r["outcome"])["brier"] for r in s) / len(s)  # noqa: E731
            lg = lambda k, s=rs: sum(B.scores(r[k], r["outcome"])["log"] for r in s) / len(s)  # noqa: E731
            ci = SB.bootstrap(rs, lambda s: sum(B.scores(r["p_bot"], r["outcome"])["brier"]
                                                 - B.scores(r["p_mkt"], r["outcome"])["brier"] for r in s) / len(s), boot)
            out.append(f"| {point} | {mo} | {len(rs)} | {len({r['cluster'] for r in rs})} | {br('p_bot'):.3f} | "
                       f"{br('p_mkt'):.3f} | {lg('p_bot'):.3f} | {lg('p_mkt'):.3f} | "
                       f"{'[%+.3f; %+.3f]' % ci if ci else '—'} |")
    out += ["", "| точка | режим | порог | сделок | событий | прибыльных | ROI | ROI 90% |", "|---|---|---|---|---|---|---|---|"]
    for point in ("t48", "t50", "все"):
        for mo in modes:
            rs = [dict(v[mo], p_quant=v[mo]["p_bot"]) for v in common if point == "все" or v[mo]["point"] == point]
            for thr in B.THRESHOLDS:
                ts = SB.trades(rs, "p_quant", thr)
                if not ts:
                    continue
                ci = SB.bootstrap(rs, lambda s: SB.roi(SB.trades(s, "p_quant", thr)), boot)
                ev = {r["cluster"] for r in rs if SB.tradable(r) and abs(r["p_quant"] - r["p_mkt"]) >= thr}
                out.append(f"| {point} | {mo} | {thr:.2f} | {len(ts)} | {len(ev)} | "
                           f"{sum(1 for t in ts if t.pnl > 0) / len(ts):.0%} | {SB.roi(ts):+.1%} | "
                           f"{'[%+.0f%%; %+.0f%%]' % (100 * ci[0], 100 * ci[1]) if ci else '—'} |")
    return "\n".join(out)


def cmd_report(a) -> int:
    rows = load_jsonl(d() / "results.jsonl")
    text = f"строк {len(rows)}; потрачено на этап №3 ${stage_spent():.4f} из ${STAGE3_CAP_USD}\n\n" + report(rows, a.boot)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0


def main() -> int:
    from forecast_bot.run import load_env_file

    load_env_file(paths.env_path())
    os.environ.setdefault("FORECAST_RESEARCH", "none")
    os.environ.setdefault("FORECAST_MODEL", "openrouter/anthropic/claude-opus-5.5")
    os.environ.setdefault("FORECAST_REASONING", "high")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select")
    tf = sub.add_parser("tg-fetch")
    tf.add_argument("--folder", type=int, default=FOLDER_ID)
    sub.add_parser("keywords")
    g = sub.add_parser("gdelt")
    g.add_argument("--days", type=int, default=3)
    g.add_argument("--per-hour", type=int, default=2)
    g.add_argument("--workers", type=int, default=4)
    f = sub.add_parser("forecast")
    f.add_argument("--modes", default="none,gdelt,tg")
    f.add_argument("--points", default="t48,t50")
    f.add_argument("--limit", type=int, default=0)
    r = sub.add_parser("report")
    r.add_argument("--out", default="")
    r.add_argument("--boot", type=int, default=SB.BOOT)
    a = ap.parse_args()
    if a.cmd == "tg-fetch":
        return asyncio.run(_tg_fetch(a))
    if a.cmd == "keywords":
        return asyncio.run(_keywords(a))
    if a.cmd == "forecast":
        return asyncio.run(run_forecast(a))
    return {"select": cmd_select, "gdelt": cmd_gdelt, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
