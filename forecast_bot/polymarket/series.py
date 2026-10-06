"""Бэктест №2: рынки Polymarket на ряды данных (крипта, доходности UST, DXY, нефть, CPI/JOLTS). Только чтение.

Событие Gamma → класс по заголовку (белый список ниже) → рынки-страйки → спецификация условия (`Spec`):
порог/корзина, тип (закрытие, касание, корзина, макро-релиз), момент резолва. Неоднозначное — не берём.

Точки t50/t48 — от ПЛАНОВОЙ `endDate`, а не `closedTime`: рынок «коснётся ли» закрывается в момент касания, и t48
от закрытия означал бы выбор момента по исходу (утечка). Рынок, закрытый до t, отбрасывается — это известно на t.

Источники резолва (описания рынков, Gamma, 06.10.2026):
- крипта «above/price on D» — закрытие 1-мин свечи Binance BTC/USDT, ETH/USDT в 12:00 ET даты D;
- крипта «hit D1-D7» — High/Low 1-мин свечей Binance за окно (12:00 AM ET первого дня … 11:59 PM ET последнего);
- DXY / WTI «hit» — 1-мин свечи Pyth (DXY; активный месяц WTI) — у нас прокси Yahoo DX-Y.NYB / CL=F по часу;
- доходности «How high/low» — Daily Treasury Par Yield Curve Rates (home.treasury.gov), любой день окна;
- CPI/JOLTS — отчёт BLS (у нас ALFRED, винтаж на дату t).
Курсы валют (только дневные Up/Down, живут ~1.5 сут) и бензин (рынков за июль–сентябрь нет) — не входят.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

from forecast_bot.polymarket import backtest as B
from forecast_bot.polymarket import markets as M
from forecast_bot.polymarket.http import get_json

EVENTS = "https://gamma-api.polymarket.com/events"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
TAGS = {  # тег Gamma → окно обхода в днях (крипта плотная: ~200 событий/сутки из-за почасовых Up/Down)
    "bitcoin": 2, "ethereum": 2, "forex": 7, "commodities": 7, "economy": 7, "finance": 3, "jobs-report": 14,
    "oil": 7, "dxy": 7,
}
MAX_STRIKES = 8  # страйков на событие — равномерно по сетке (не по цене: выбор по цене на t смещал бы выборку)

# Классы: (regex по заголовку события, класс, актив). Всё, что не совпало, — вне выборки.
CLASSES = [
    (r"^(Bitcoin|Ethereum) above ___ on [A-Z][a-z]+ \d{1,2}\?$", "crypto_close"),
    (r"^(Bitcoin|Ethereum) price on [A-Z][a-z]+ \d{1,2}\?$", "crypto_bracket"),
    (r"^What price will (Bitcoin|Ethereum) hit (?:[A-Z][a-z]+ \d{1,2}-\d{1,2}|in [A-Z][a-z]+)\??$", "crypto_hit"),
    (r"^What will US Dollar Index \(DXY\) hit (?:Week of .+|in .+)\?$", "dxy_hit"),
    (r"^What will WTI Crude Oil \(WTI\) hit (?:Week of .+|in .+)\?$", "wti_hit"),
    (r"^How (?:high|low) will (\d+)-year Treasury yield (?:go|get) in [A-Z][a-z]+\?$", "ust_hit"),
    (r"^[A-Z][a-z]+ Inflation US - (Monthly|Annual)$", "cpi"),
    (r"^Core CPI (MoM|YoY) - [A-Z][a-z]+ \d{4}$", "core_cpi"),
    (r"^JOLTS Job Openings\s*[:—–-]\s*[A-Z][a-z]+ \d{4}$", "jolts"),
]
FAMILY = {"crypto_close": "крипта", "crypto_bracket": "крипта", "crypto_hit": "крипта", "dxy_hit": "DXY",
          "wti_hit": "нефть", "ust_hit": "UST", "cpi": "макро", "core_cpi": "макро", "jolts": "макро"}
ASSETS = {"bitcoin": "binance:BTCUSDT", "ethereum": "binance:ETHUSDT"}
UST_COL = {"2": "2 Yr", "5": "5 Yr", "10": "10 Yr", "30": "30 Yr"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
INF = float("inf")


def classify(title: str) -> Optional[tuple[str, str]]:
    """(класс, ключ ряда) или None."""
    t = (title or "").strip()
    for rx, cls in CLASSES:
        m = re.match(rx, t)
        if not m:
            continue
        if cls.startswith("crypto"):
            return cls, ASSETS[m.group(1).lower()]
        if cls == "dxy_hit":
            return cls, "yahoo:DX-Y.NYB"
        if cls == "wti_hit":
            return cls, "yahoo:CL=F"
        if cls == "ust_hit":
            tenor = m.group(1)
            return (cls, f"treasury:{UST_COL[tenor]}") if tenor in UST_COL else None
        if cls == "cpi":
            return cls, "macro:cpi_mom" if m.group(1) == "Monthly" else "macro:cpi_yoy"
        if cls == "core_cpi":
            return cls, "macro:core_mom" if m.group(1) == "MoM" else "macro:core_yoy"
        if cls == "jolts":
            return cls, "macro:jolts"
    return None


@dataclass
class Spec:
    kind: str                 # close_above | bracket | hit_high | hit_low | close_hit_high | close_hit_low | bucket
    lo: float = -INF          # bracket/bucket: [lo, hi); close_above/hit: порог в lo
    hi: float = INF
    target_month: Optional[str] = None  # макро: "YYYY-MM"


def _num(s: str) -> float:
    return float(s.replace(",", "").replace("$", "").strip())


def parse_strike(cls: str, item: str, question: str, event_title: str,
                 end: Optional[datetime] = None) -> Optional[Spec]:
    """Условие рынка по `groupItemTitle` (+вопрос для проверки). Неоднозначное → None."""
    it = (item or "").strip()
    q = (question or "").lower()
    if cls == "crypto_close":
        if not re.fullmatch(r"[\d,]+", it) or "above" not in q:
            return None
        return Spec("close_above", lo=_num(it))
    if cls == "crypto_bracket":
        m = re.fullmatch(r"<\s*([\d,]+)", it)
        if m:
            return Spec("bracket", hi=_num(m.group(1)))
        m = re.fullmatch(r">\s*([\d,]+)", it)
        if m:
            return Spec("bracket", lo=_num(m.group(1)))
        m = re.fullmatch(r"([\d,]+)\s*-\s*([\d,]+)", it)
        if m:  # «ровно на границе — в верхнюю корзину» (правило рынка) → [lo, hi)
            return Spec("bracket", lo=_num(m.group(1)), hi=_num(m.group(2)))
        return None
    if cls in ("crypto_hit", "dxy_hit", "wti_hit"):
        m = re.fullmatch(r"([↑↓])\s*\$?([\d,]+(?:\.\d+)?)", it)
        if not m:
            return None
        return Spec("hit_high" if m.group(1) == "↑" else "hit_low", lo=_num(m.group(2)))
    if cls == "ust_hit":
        m = re.fullmatch(r"(Below\s+)?(\d+(?:\.\d+)?)%", it)
        if not m:
            return None
        if m.group(1):
            return Spec("close_hit_low", lo=float(m.group(2)))      # «ниже порога в любой день окна»
        return Spec("close_hit_high", lo=float(m.group(2)))         # «на уровне или выше в любой день окна»
    if cls in ("cpi", "core_cpi", "jolts"):
        month = _macro_month(event_title, question, end)
        if month is None:
            return None
        b = _bucket(cls, it)
        return Spec("bucket", lo=b[0], hi=b[1], target_month=month) if b else None
    return None


def _macro_month(event_title: str, question: str, end: Optional[datetime] = None) -> Optional[str]:
    m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\b"
                  r"(?:\s+(\d{4}))?", event_title or "")
    if not m:
        return None
    year = m.group(2)
    if not year:
        y = re.search(r"\b(20\d\d)\b", question or "")
        year = y.group(1) if y else None
    mon = MONTHS[m.group(1).lower()]
    if not year and end is not None:  # «August Inflation US» без года: данные за месяц, вышедшие к концу рынка
        year = end.year if mon <= end.month else end.year - 1
    return f"{year}-{mon:02d}" if year else None


def _bucket(cls: str, it: str) -> Optional[tuple[float, float]]:
    """Корзина [lo, hi) в единицах публикации. CPI — %, округлённые до 0.1 → ±0.05; JOLTS — тысячи вакансий."""
    s = it.replace("−", "-").replace(" ", "")
    if cls == "jolts":
        m = re.fullmatch(r"<(\d+(?:\.\d+)?)M", s)
        if m:
            return -INF, float(m.group(1)) * 1000
        m = re.fullmatch(r"(?:≥|>=|>)(\d+(?:\.\d+)?)M|(\d+(?:\.\d+)?)M\+", s)
        if m:
            return float(m.group(1) or m.group(2)) * 1000, INF
        m = re.fullmatch(r"(\d+(?:\.\d+)?)Mto(\d+(?:\.\d+)?)M", s)
        if m:
            return float(m.group(1)) * 1000, float(m.group(2)) * 1000
        return None
    m = re.fullmatch(r"≤(-?\d+(?:\.\d+)?)%", s)
    if m:
        return -INF, round(float(m.group(1)) + 0.05, 4)
    m = re.fullmatch(r"(?:≥(-?\d+(?:\.\d+)?)%|(-?\d+(?:\.\d+)?)%\+)", s)
    if m:
        return round(float(m.group(1) or m.group(2)) - 0.05, 4), INF
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)%", s)
    if m:
        v = float(m.group(1))
        return round(v - 0.05, 4), round(v + 0.05, 4)
    return None


@dataclass
class SeriesMarket(M.Market):
    event_id: str = ""
    event_title: str = ""
    cls: str = ""
    key: str = ""              # ключ ряда (binance:BTCUSDT, yahoo:CL=F, treasury:10 Yr, macro:cpi_mom)
    end_planned: Optional[datetime] = None
    spec: Optional[Spec] = None
    rule: str = ""

    @property
    def family(self) -> str:
        return FAMILY.get(self.cls, self.cls)

    def resolve_at(self) -> datetime:
        """Момент, к которому относится условие. UST: дневная ставка последней даты окна публикуется ~18:00 ET
        (берём 23:00 UTC с запасом); остальное — плановый конец рынка."""
        if self.cls == "ust_hit":
            d = self.end_planned.astimezone(UTC).date()
            return datetime(d.year, d.month, d.day, 23, 0, tzinfo=UTC)
        return self.end_planned

    def cluster(self) -> str:
        """Кластер для бутстрэпа: крипта — актив × неделя резолва (окна соседних дней перекрываются),
        остальное — событие."""
        if self.family == "крипта":
            iso = self.end_planned.isocalendar()
            return f"{self.key}:{iso[0]}-W{iso[1]:02d}"
        return f"ev{self.event_id}"

    def to_json(self) -> str:
        d = asdict(self)
        d["start"], d["closed"] = self.start.isoformat(), self.closed.isoformat()
        d["end_planned"] = self.end_planned.isoformat() if self.end_planned else None
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "SeriesMarket":
        d = json.loads(line)
        for k in ("start", "closed", "end_planned"):
            d[k] = M.parse_ts(d[k]) if d.get(k) else None
        d["history"] = [tuple(x) for x in d.get("history") or []]
        d["spec"] = Spec(**d["spec"]) if d.get("spec") else None
        return cls(**d)


def points(m: SeriesMarket) -> dict[str, datetime]:
    """t50/t48 от плановой endDate (не closedTime). Рынок, уже закрытый на t, — без этой точки."""
    out = B.points(m.start, m.end_planned)
    return {p: t for p, t in out.items() if m.closed > t}


def price_point(m: M.Market, t: datetime) -> Optional[tuple[datetime, float]]:
    """(момент, цена YES) последней точки истории CLOB строго до t. Модель сравнивается с рынком при равной
    информации: ряд для неё обрезается этим же моментом, а не t."""
    prev = [(ts, p) for ts, p in m.history if ts < t.timestamp()]
    if not prev:
        return None
    ts, p = prev[-1]
    return datetime.fromtimestamp(ts, UTC), p


def from_event(ev: dict) -> list[SeriesMarket]:
    """Рынки события, годные для бэктеста: класс известен, условие разобрано, срок жизни 3–30 дней (как в 3A)."""
    c = classify(ev.get("title") or "")
    if c is None:
        return []
    cls, key = c
    out: list[SeriesMarket] = []
    seen: set[tuple] = set()
    for raw in sorted(ev.get("markets") or [], key=lambda r: r.get("startDate") or ""):
        base = M.from_gamma(raw)
        if base is None:
            continue
        end = M.parse_ts(raw.get("endDate"))
        if end is None:
            continue
        spec = parse_strike(cls, raw.get("groupItemTitle") or "", raw.get("question") or "", ev.get("title") or "", end)
        if spec is None:
            continue
        sig = (spec.kind, spec.lo, spec.hi)
        if sig in seen:  # тот же страйк, добавленный позже (у «hit» бывает) — оставляем самый ранний
            continue
        seen.add(sig)
        sm = SeriesMarket(**{k: getattr(base, k) for k in base.__dataclass_fields__})
        sm.event_id, sm.event_title, sm.cls, sm.key = str(ev.get("id")), ev.get("title") or "", cls, key
        sm.end_planned, sm.spec, sm.rule = end, spec, (raw.get("description") or "")[:1200]
        life = (end - sm.start).total_seconds() / 86400
        if life > M.MAX_LIFE_DAYS or life < M.MIN_LIFE_DAYS:
            continue
        out.append(sm)
    return thin(out)


def thin(ms: list[SeriesMarket], k: int = MAX_STRIKES) -> list[SeriesMarket]:
    """До k страйков на событие равномерно по сетке порогов (по возрастанию lo, затем hi)."""
    if len(ms) <= k:
        return ms
    ms = sorted(ms, key=lambda m: (m.spec.kind, m.spec.lo, m.spec.hi))
    idx = sorted({round(i * (len(ms) - 1) / (k - 1)) for i in range(k)})
    return [ms[i] for i in idx]


def iter_events(tag: str, end_min: datetime, end_max: datetime, window_days: int, log=print) -> Iterator[dict]:
    """Закрытые события тега окнами по endDate (от свежих к старым), внутри окна — страницы до offset 2000."""
    w_hi = end_max
    while w_hi > end_min:
        w_lo = max(end_min, w_hi - timedelta(days=window_days))
        for offset in range(0, M.MAX_OFFSET + 1, 100):
            try:
                rows = get_json(EVENTS, {"tag_slug": tag, "closed": "true", "limit": 100, "offset": offset,
                                         "end_date_min": w_lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "end_date_max": w_hi.strftime("%Y-%m-%dT%H:%M:%SZ")})
            except RuntimeError as exc:
                log(f"{tag} {w_lo:%m-%d}…{w_hi:%m-%d} offset {offset}: {str(exc)[:80]} — окно пропущено")
                break
            if not rows:
                break
            yield from rows
            if len(rows) < 100:
                break
            if offset + 100 > M.MAX_OFFSET:
                log(f"{tag} {w_lo:%m-%d}…{w_hi:%m-%d}: упёрлись в offset 2000 — окно неполное")
        w_hi = w_lo
