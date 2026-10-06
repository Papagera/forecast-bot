"""Закрытые бинарные рынки Polymarket (Gamma) и история цены YES (CLOB prices-history). Только чтение."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

from forecast_bot.polymarket.http import get_json

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
MAX_LIFE_DAYS = 30
MIN_LIFE_DAYS = 3   # короче — нет обеих точек (t48 нужен рынок > 60 ч); спорт «на матч» живёт ~1 ч
PAGE = 100          # Gamma отдаёт не больше 100 строк на запрос (живьём 05.10.2026, limit=500 → 100)

# Ставки taker fee по категории (docs.polymarket.com/polymarket-learn/trading/fees, 04.10.2026):
# fee = C × feeRate × p × (1 − p). Категорию даёт поле Gamma `feeType` вида «politics_fees».
FEE_RATES = {"crypto": 0.07, "sports": 0.05, "economics": 0.05, "culture": 0.05, "weather": 0.05, "other": 0.05,
             "finance": 0.04, "politics": 0.04, "mentions": 0.04, "tech": 0.04, "geopolitic": 0.0}
DEFAULT_FEE = 0.05


def parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fee_rate(fee_type: Optional[str], fees_enabled: Optional[bool] = True) -> float:
    if fees_enabled is False:
        return 0.0
    t = (fee_type or "").lower()
    return next((r for k, r in FEE_RATES.items() if t.startswith(k)), DEFAULT_FEE)


@dataclass
class Market:
    id: str
    question: str
    slug: str
    description: str
    start: datetime
    closed: datetime
    volume: float
    liquidity: Optional[float]
    fee_rate: float
    neg_risk: bool
    yes_token: str
    outcome: int                       # 1 — победило Yes, 0 — No
    history: list[tuple[int, float]] = field(default_factory=list)

    @property
    def life_days(self) -> float:
        return (self.closed - self.start).total_seconds() / 86400

    @property
    def url(self) -> str:
        return f"https://polymarket.com/market/{self.slug}"

    def price_before(self, ts: float) -> Optional[float]:
        """Последняя цена YES строго ДО момента ts — защита от утечки будущего."""
        prev = [p for t, p in self.history if t < ts]
        return prev[-1] if prev else None

    def to_json(self) -> str:
        d = asdict(self)
        d["start"], d["closed"] = self.start.isoformat(), self.closed.isoformat()
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "Market":
        d = json.loads(line)
        d["start"], d["closed"] = parse_ts(d["start"]), parse_ts(d["closed"])
        d["history"] = [tuple(x) for x in d.get("history") or []]
        return cls(**d)


def from_gamma(m: dict, max_life_days: float = MAX_LIFE_DAYS) -> Optional[Market]:
    """Рынок годится для бэктеста: бинарный Yes/No, однозначный итог, срок жизни ≤ 30 дней (№3 «Украина» — ≤ 60)."""
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
        tokens = json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    if [o.lower() for o in outcomes] != ["yes", "no"] or len(tokens) != 2 or len(prices) != 2:
        return None
    if m.get("umaResolutionStatus") != "resolved" or prices not in (["1", "0"], ["0", "1"]):
        return None
    start, closed = parse_ts(m.get("startDate")), parse_ts(m.get("closedTime"))
    if not start or not closed or closed <= start:
        return None
    life = (closed - start).total_seconds() / 86400
    if life > max_life_days or life < MIN_LIFE_DAYS:
        return None
    return Market(id=str(m.get("id")), question=m.get("question") or "", slug=m.get("slug") or "",
                  description=(m.get("description") or "")[:4000], start=start, closed=closed,
                  volume=float(m.get("volumeNum") or 0), liquidity=m.get("liquidityNum"),
                  fee_rate=fee_rate(m.get("feeType"), m.get("feesEnabled")), neg_risk=bool(m.get("negRisk")),
                  yes_token=str(tokens[0]), outcome=1 if prices == ["1", "0"] else 0)


MAX_OFFSET = 2000  # Gamma: offset > 2000 → 422 Unprocessable Entity (живьём 05.10.2026)


def iter_closed(end_min: str, end_max: str, page: int = PAGE, window_days: int = 3) -> Iterator[dict]:
    """Закрытые рынки окнами по endDate (от свежих к старым): внутри окна — страницы до MAX_OFFSET."""
    from datetime import timedelta

    lo, hi = parse_ts(end_min), parse_ts(end_max)
    w_hi = hi
    while w_hi > lo:
        w_lo = max(lo, w_hi - timedelta(days=window_days))
        for offset in range(0, MAX_OFFSET + 1, page):
            try:
                # без order: сортировка endDate на окнах до ~02.08.2026 даёт 500 (живьём 05.10), окна и так задают порядок
                rows = get_json(GAMMA, {"closed": "true", "limit": page, "offset": offset,
                                        "end_date_min": w_lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                        "end_date_max": w_hi.strftime("%Y-%m-%dT%H:%M:%SZ")})
            except RuntimeError as exc:
                # 422 — глубже окна Gamma не пускает; 5xx после повторов — разовый сбой. В обоих случаях
                # переходим к следующему окну, а не роняем ночной обход.
                print(f"окно {w_lo:%Y-%m-%d}…{w_hi:%Y-%m-%d}, offset {offset}: {str(exc)[:80]}", flush=True)
                break
            if not rows:
                break
            yield from rows
            if len(rows) < page:
                break
        w_hi = w_lo


HISTORY_CHUNK_S = 7 * 86400  # длинный диапазон startTs/endTs → 400 (живьём 05.10.2026); окно в неделю проходит


def load_history(m: Market, fidelity_min: int = 60) -> list[tuple[int, float]]:
    """Часовая история цены YES. interval=max у рынков, закрытых до ~июля 2026, отдаёт пусто (живьём 05.10.2026:
    0 точек против 160–195 по явному диапазону) — тогда добираем окнами startTs/endTs."""
    d = get_json(CLOB_HISTORY, {"market": m.yes_token, "interval": "max", "fidelity": fidelity_min})
    pts = {int(x["t"]): float(x["p"]) for x in (d or {}).get("history", [])}
    if len(pts) < 3:
        lo, hi = int(m.start.timestamp()), int(m.closed.timestamp())
        while lo < hi:
            end = min(hi, lo + HISTORY_CHUNK_S)
            d = get_json(CLOB_HISTORY, {"market": m.yes_token, "startTs": lo, "endTs": end, "fidelity": fidelity_min})
            pts.update({int(x["t"]): float(x["p"]) for x in (d or {}).get("history", [])})
            lo = end
    return sorted(pts.items())
