"""Бэктест №2 Polymarket «ряды данных»: quant / quant + LLM по GDELT / цена рынка. Только чтение, ничего не торгует.

    .venv/bin/python tools/polymarket_series.py select            # рынки + история цены (CLOB), возобновляемо
    .venv/bin/python tools/polymarket_series.py quant             # (a) по всем точкам — бесплатно
    .venv/bin/python tools/polymarket_series.py check             # сверка: наш ряд даёт тот же итог, что Polymarket
    .venv/bin/python tools/polymarket_series.py gdelt             # заголовки GDELT до t по группам (бесплатно)
    .venv/bin/python tools/polymarket_series.py llm [--limit N]   # (b) — платно, потолок этапа $20
    .venv/bin/python tools/polymarket_series.py report [--out файл.md]

Данные — ~/.forecast-bot/polymarket/series/ (вне репо). ИИ — приложение `polymarket`, пользователи `pm2:*`.
"""
from __future__ import annotations

import os

os.environ["FORECAST_APP"] = "polymarket"  # ДО импорта forecast_bot: потолок приложения читается при импорте

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import zlib  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forecast_bot import paths  # noqa: E402
from forecast_bot.polymarket import backtest as B, gdelt, markets as M, series as S  # noqa: E402
from forecast_bot.polymarket import series_backtest as SB, series_data as SD, series_quant as Q  # noqa: E402

UTC = timezone.utc
END_MIN = datetime(2026, 7, 3, tzinfo=UTC)
END_MAX = datetime(2026, 10, 1, tzinfo=UTC)
DATA_START = datetime(2023, 7, 1, tzinfo=UTC)


def d() -> Path:
    p = SB.data_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


def store() -> SD.Store:
    return SD.Store(d() / "cache", DATA_START, END_MAX + timedelta(days=2))


def load_markets() -> list[S.SeriesMarket]:
    p = d() / "markets.jsonl"
    if not p.exists():
        return []
    return [S.SeriesMarket.from_json(l) for l in p.read_text().splitlines() if l.strip()]


def group_key(m: S.SeriesMarket) -> str:
    if m.cls in ("crypto_close", "crypto_bracket"):  # одна дата резолва одного актива — одна группа
        return f"{m.key}@{m.end_planned:%Y%m%d%H}"
    return f"ev{m.event_id}"


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


# ─────────────────────────── select ────────────────────────────────
def cmd_select(a) -> int:
    path = d() / "markets.jsonl"
    have = {m.id: m for m in load_markets()}
    print(f"уже в файле: {len(have)}", flush=True)
    events_seen, per_cls = set(), Counter()
    fresh: list[S.SeriesMarket] = []
    for tag, win in S.TAGS.items():
        n_ev = 0
        for ev in S.iter_events(tag, END_MIN, END_MAX, win):
            if ev.get("id") in events_seen:
                continue
            events_seen.add(ev.get("id"))
            for m in S.from_event(ev):
                if m.id in have or any(x.id == m.id for x in fresh):
                    continue
                fresh.append(m)
                per_cls[m.cls] += 1
            n_ev += 1
        print(f"тег {tag}: событий {n_ev}, рынков пока {len(fresh)}", flush=True)
    print("новых рынков по классам:", dict(per_cls), flush=True)
    with path.open("a") as fh:
        for i, m in enumerate(fresh, 1):
            try:
                m.history = M.load_history(m)
            except Exception as exc:  # одна история не скачалась — рынок пропускаем, при повторе докачается
                print(f"история {m.id}: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                continue
            if len(m.history) < 3:
                continue
            fh.write(m.to_json() + "\n")
            fh.flush()
            if i % 100 == 0:
                print(f"история {i}/{len(fresh)}", flush=True)
    print(f"готово → {path}")
    return 0


# ─────────────────────────── quant ─────────────────────────────────
def macro_seed(m: S.SeriesMarket, t: datetime) -> int:
    return zlib.crc32(f"{m.key}|{m.spec.target_month}|{t.isoformat()}".encode())


def dist_for(m: S.SeriesMarket, t: datetime, st: SD.Store, cache: dict):
    """Распределение (Dist/MacroDist) или причина пропуска. Кэш по (ряд, t, момент резолва)."""
    if m.key.startswith("macro:"):
        ck = (m.key, m.spec.target_month, t)
        if ck not in cache:
            cache[ck] = Q.macro_dist(m.key, m.spec.target_month, t, st, seed=macro_seed(m, t))
        return cache[ck]
    ck = (m.key, t, m.resolve_at())
    if ck not in cache:
        bars = st.bars(m.key)
        freq = "day" if m.key.startswith("treasury:") else "hour"
        dist = Q.price_dist(bars, t.timestamp(), m.resolve_at().timestamp(), freq)
        cache[ck] = (dist, "" if dist else "мало истории до t")
    return cache[ck]


def quant_rows(markets: list[S.SeriesMarket], st: SD.Store) -> tuple[list[dict], Counter]:
    rows, skip, cache = [], Counter(), {}
    for m in markets:
        for point, t in S.points(m).items():
            if t < B.CUTOFF:
                skip["t до cutoff"] += 1
                continue
            px = S.price_point(m, t)
            if px is None:
                skip["нет цены до t"] += 1
                continue
            t_info, p_mkt = px
            try:
                # quant видит ряд только до момента цены рынка, с которой сравнивается: история CLOB часовая,
                # цена «строго до t» — на час старше t; ряд до самого t дал бы модели лишний час информации
                dist, why = dist_for(m, t_info, st, cache)
            except Exception as exc:
                skip[f"ряд: {type(exc).__name__}"] += 1
                continue
            if dist is None:
                skip[why] += 1
                continue
            if not m.key.startswith("macro:") and Q.already_hit(m.spec, st.bars(m.key).between(m.start.timestamp(),
                                                                                                  t.timestamp())):
                skip["порог пройден до t"] += 1
                continue
            p = Q.macro_prob(m.spec, dist) if m.key.startswith("macro:") else Q.prob(m.spec, dist)
            if p is None:
                skip["тип не поддержан"] += 1
                continue
            rows.append({"market": m.id, "event": m.event_id, "question": m.question, "cls": m.cls,
                         "family": m.family, "key": m.key, "cluster": m.cluster(), "group": group_key(m),
                         "point": point, "t": t.isoformat(), "t_info": t_info.isoformat(), "p_mkt": p_mkt,
                         "p_quant": p, "outcome": m.outcome,
                         "fee_rate": m.fee_rate, "volume": m.volume, "segment": B.segment(m.volume)})
    return rows, skip


def cmd_quant(_a) -> int:
    markets = load_markets()
    rows, skip = quant_rows(markets, store())
    with (d() / "quant.jsonl").open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"рынков {len(markets)}, строк {len(rows)}; пропуски: {dict(skip)}")
    return 0


# ─────────────────────────── check ─────────────────────────────────
_MINUTE: dict = {}


def our_outcome(m: S.SeriesMarket, st: SD.Store):
    """Итог по НАШЕМУ ряду (после резолва) — для сверки источника, не для прогноза."""
    sp = m.spec
    if m.key.startswith("macro:"):
        sid, _, typ = Q.MACRO_SERIES[m.key]
        today = datetime.now(UTC)
        rows = st.vintage(sid, today.date() - timedelta(days=1))
        y, mo = map(int, sp.target_month.split("-"))
        have = {Q._mi(x): v for x, v in rows}
        tgt = y * 12 + mo - 1
        if tgt not in have:
            return None
        if typ == "mom":
            v = 100 * (have[tgt] / have[tgt - 1] - 1)
        elif typ == "yoy":
            v = 100 * (have[tgt] / have[tgt - 12] - 1)
        else:
            v = have[tgt]
        v = round(v, 1) if typ != "level" else v
        return int(sp.lo <= v < sp.hi)
    if m.cls in ("crypto_close", "crypto_bracket"):
        mk = (m.key, m.end_planned)
        if mk not in _MINUTE:  # одна минута Binance на дату резолва, а не на каждый страйк
            _MINUTE[mk] = SD.binance_minute_close(m.key.split(":", 1)[1], m.end_planned)
        px = _MINUTE[mk]
        if px is None:
            return None
        return int(px > sp.lo) if sp.kind == "close_above" else int(sp.lo <= px < sp.hi)
    seen = st.bars(m.key).between(m.start.timestamp(), m.resolve_at().timestamp())
    if not len(seen):
        return None
    if sp.kind in ("hit_high", "close_hit_high"):
        return int(seen.high.max() >= sp.lo)
    if sp.kind == "hit_low":
        return int(seen.low.min() <= sp.lo)
    return int(seen.low.min() < sp.lo)


def cmd_check(_a) -> int:
    st = store()
    stat = defaultdict(Counter)
    for m in load_markets():
        try:
            y = our_outcome(m, st)
        except Exception as exc:
            stat[m.cls][f"ошибка {type(exc).__name__}"] += 1
            continue
        if y is None:
            stat[m.cls]["нет данных"] += 1
            continue
        stat[m.cls]["совпало" if y == m.outcome else "расходится"] += 1
    out = {}
    for cls, c in sorted(stat.items()):
        ok, bad = c["совпало"], c["расходится"]
        out[cls] = {**c, "доля": round(ok / (ok + bad), 3) if ok + bad else None}
        print(cls, dict(c), f"совпадение {out[cls]['доля']}")
    (d() / "check.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


# ─────────────────────────── gdelt ─────────────────────────────────
def group_points(rows: list[dict]) -> dict[str, tuple[str, datetime]]:
    """(группа|точка) → (ключ ряда, t)."""
    out = {}
    for r in rows:
        out[f"{r['group']}|{r['point']}"] = (r["key"], datetime.fromisoformat(r["t"]))
    return out


def cmd_gdelt(a) -> int:
    from concurrent.futures import ThreadPoolExecutor

    from forecast_bot.polymarket import gdelt_files as GF
    from forecast_bot.polymarket import series_llm as L

    gp = group_points(load_jsonl(d() / "quant.jsonl"))
    points = [(k, L.query_for(key), t) for k, (key, t) in gp.items()]
    by_file = GF.plan(points, a.days, a.per_hour)
    log_path = d() / "gdelt_files_log.jsonl"
    done = {r["file"] for r in load_jsonl(log_path)}
    todo = sorted(ts for ts in by_file if ts.strftime("%Y%m%d%H%M%S") not in done)
    print(f"точек {len(points)}, файлов {len(by_file)}, уже {len(done)}, качать {len(todo)}", flush=True)

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

    errors = 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool, log_path.open("a") as log:
        for i, fut in enumerate([pool.submit(work, ts) for ts in todo], 1):
            try:
                ts, ok, hits = fut.result()
            except Exception as exc:
                errors += 1
                print(f"ошибка файла: {str(exc)[:100]}", flush=True)
                continue
            log.write(json.dumps({"file": ts.strftime("%Y%m%d%H%M%S"), "ok": ok, "hits": hits}, ensure_ascii=False)
                      + "\n")
            log.flush()
            if i % 100 == 0:
                print(f"{i}/{len(todo)}", flush=True)
    c = GF.Collector(keep=a.keep)
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
    print(f"кэш: {len(points)} точек, с новостями {sum(1 for k, _, _ in points if c.articles(k))}, "
          f"ошибок файлов {errors}")
    return 0


def load_news() -> dict[str, list]:
    out = {}
    for r in load_jsonl(d() / "gdelt.jsonl"):
        t = datetime.fromisoformat(r["t"])
        # строгая отсечка ещё раз — на случай чужого/старого кэша
        out[r["key"]] = [gdelt.Article(datetime.fromisoformat(x["seen"]), x["title"], x["url"], x["domain"], "")
                         for x in r["articles"] if datetime.fromisoformat(x["seen"]) < t]
    return out


# ─────────────────────────── llm ───────────────────────────────────
def cond_text(m: S.SeriesMarket) -> str:
    sp = m.spec
    f = (lambda v: f"{v:,.2f}") if (m.key.startswith("treasury:") or m.key == "yahoo:DX-Y.NYB") else \
        (lambda v: f"{v:,.4g}" if abs(v) < 100 else f"{v:,.0f}")
    when = f"{m.resolve_at():%Y-%m-%d %H:%M} UTC"
    if sp.kind == "close_above":
        return f"value at {when} above {f(sp.lo)}"
    if sp.kind == "bracket":
        lo = "−∞" if sp.lo == -S.INF else f(sp.lo)
        hi = "+∞" if sp.hi == S.INF else f(sp.hi)
        return f"value at {when} in [{lo}, {hi})"
    if sp.kind in ("hit_high", "close_hit_high"):
        return f"reaches ≥ {f(sp.lo)} before {when}"
    if sp.kind in ("hit_low", "close_hit_low"):
        return f"falls below {f(sp.lo)} before {when}"
    lo = "−∞" if sp.lo == -S.INF else f"{sp.lo:g}"
    hi = "+∞" if sp.hi == S.INF else f"{sp.hi:g}"
    return f"{sp.target_month} value in [{lo}, {hi})"


def baseline_text(m: S.SeriesMarket, dist) -> str:
    if isinstance(dist, Q.MacroDist):
        return (f"Target month {m.spec.target_month}. Baseline distribution of the published value: mean "
                f"{dist.center:.3f}, std {dist.sd:.3f} (from the last {Q.MACRO_HIST} months as published by the "
                f"forecast date).")
    unit = "percentage points" if dist.diff else "% (log change)"
    sd = dist.sd if dist.diff else dist.sd * 100
    t_last = datetime.fromtimestamp(dist.info["t_last"], UTC)
    return (f"Last known value {dist.s0:,.4f} at {t_last:%Y-%m-%d %H:%M} UTC; horizon to resolution "
            f"{dist.horizon_s / 3600:.0f} h; historical std of the change over this horizon: {sd:.3f} {unit} "
            f"({dist.info['analogs']} historical windows).")


async def run_llm(a) -> int:
    from forecast_bot import ai_guard, guarded_llm
    from forecast_bot.polymarket import series_llm as L

    if guarded_llm.APP != B.APP:  # траты №2 обязаны лечь в приложение polymarket — иначе потолок их не увидит
        raise RuntimeError(f"guarded_llm.APP={guarded_llm.APP!r}, нужен {B.APP!r}: FORECAST_APP задан поздно")
    ai_guard.APP_LIMITS[B.APP] = {"day_usd": SB.STAGE2_DAY_USD}  # только на прогон (как 3A)
    guarded_llm.start_run(max(0.0, SB.stage_budget_left()))
    markets = {m.id: m for m in load_markets()}
    qrows = load_jsonl(d() / "quant.jsonl")
    news = load_news()
    out_path = d() / "llm.jsonl"
    done = {(r["group"], r["point"]) for r in load_jsonl(out_path)}
    by_group = defaultdict(list)
    for r in qrows:
        by_group[(r["group"], r["point"])].append(r)
    order = sorted(by_group, key=lambda k: (k[1] != a.first_point, k[0]))
    st, cache, n, stats = store(), {}, 0, Counter()
    for gk in order:
        if gk in done:
            continue
        if a.limit and n >= a.limit:
            break
        if not any(SB.tradable(r) for r in by_group[gk]):
            stats["только «микро» — вне сравнения"] += 1  # цена там — котировка открытия; вызов был бы впустую
            continue
        news_key = f"{gk[0]}|{gk[1]}"
        if news_key not in news:
            stats["нет кэша GDELT"] += 1
            continue
        if SB.stage_budget_left() <= 0:
            print(f"потолок этапа ${SB.STAGE2_CAP_USD} исчерпан — стоп")
            break
        rs = by_group[gk]
        ms = [markets[r["market"]] for r in rs]
        t = datetime.fromisoformat(rs[0]["t"])
        t_info = min(datetime.fromisoformat(r["t_info"]) for r in rs)  # момент цены рынка = граница информации
        dist, _ = dist_for(ms[0], datetime.fromisoformat(rs[0]["t_info"]), st, cache)
        arts = [x for x in news[news_key] if x.seen < t_info]
        view = L.GroupView(key=ms[0].key, t=t_info, titles=sorted({m.event_title for m in ms}), rule=ms[0].rule,
                           baseline=baseline_text(ms[0], dist),
                           strikes=[(cond_text(m), r["p_quant"]) for m, r in zip(ms, rs)],
                           news=gdelt.as_research(arts, limit=15))
        user = f"{SB.LEDGER_PREFIX}:{gk[0]}-{gk[1]}"
        t0 = time.time()
        rec = {"group": gk[0], "point": gk[1], "t": t.isoformat(), "n_news": len(arts)}
        try:
            shift, vol, reason = await L.adjust(view, user)
        except L.BadAnswer as exc:
            rec.update(error=f"ответ: {exc}")
        except Exception as exc:
            rec.update(error=f"{type(exc).__name__}: {str(exc)[:200]}")
            if guarded_llm.BUDGET_HITS.pop(user, None):
                print("лимит ai_guard — стоп")
                break
        else:
            probs = {}
            for m, r in zip(ms, rs):
                probs[r["market"]] = (Q.macro_prob(m.spec, dist, shift, vol) if isinstance(dist, Q.MacroDist)
                                      else Q.prob(m.spec, dist, shift, vol))
            rec.update(shift_sigma=shift, vol_mult=vol, reason=reason, p_llm=probs)
        cost, calls = ai_guard.spent_by_user(f"{B.APP}:{user}", t0)
        rec.update(cost_usd=cost, llm_calls=calls)
        with out_path.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n += 1
        stats["ошибка" if "error" in rec else "ok"] += 1
        print(f"{gk[0]} {gk[1]}: {rec.get('shift_sigma', '—')} / {rec.get('vol_mult', '—')} ${cost:.4f} "
              f"{rec.get('error', '')[:100]}", flush=True)
    print("итог:", dict(stats), f"потрачено на этап №2 ${SB.stage_spent():.4f} из ${SB.STAGE2_CAP_USD}")
    return 0


# ─────────────────────────── report ────────────────────────────────
def joined_rows() -> list[dict]:
    rows = load_jsonl(d() / "quant.jsonl")
    llm = {}
    for r in load_jsonl(d() / "llm.jsonl"):
        for mid, p in (r.get("p_llm") or {}).items():
            llm[(mid, r["point"])] = p
    for r in rows:
        r["p_llm"] = llm.get((r["market"], r["point"]))
    return rows


def cmd_report(a) -> int:
    rows = joined_rows()
    head = (f"строк {len(rows)}; с поправкой LLM {sum(1 for r in rows if r['p_llm'] is not None)}; "
            f"потрачено на этап №2 ${SB.stage_spent():.4f} из ${SB.STAGE2_CAP_USD}\n")
    text = head + "\n" + SB.report(rows, boot=a.boot)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0


def main() -> int:
    from forecast_bot.run import load_env_file

    load_env_file(paths.env_path())
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select")
    sub.add_parser("quant")
    sub.add_parser("check")
    g = sub.add_parser("gdelt")
    g.add_argument("--days", type=int, default=3)
    g.add_argument("--per-hour", type=int, default=2)
    g.add_argument("--workers", type=int, default=4)
    g.add_argument("--keep", type=int, default=15)
    lm = sub.add_parser("llm")
    lm.add_argument("--limit", type=int, default=0)
    lm.add_argument("--first-point", default="t48")
    r = sub.add_parser("report")
    r.add_argument("--out", default="")
    r.add_argument("--boot", type=int, default=SB.BOOT)
    a = ap.parse_args()
    if a.cmd == "llm":
        return asyncio.run(run_llm(a))
    return {"select": cmd_select, "quant": cmd_quant, "check": cmd_check, "gdelt": cmd_gdelt,
            "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
