"""Глубина архива GDELT DOC 2.0 и строгость отсечки: сколько статей в окнах на разные даты. Только чтение."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forecast_bot.polymarket import gdelt  # noqa: E402
from forecast_bot.polymarket.http import get_json  # noqa: E402


def main() -> int:
    q = "Federal Reserve interest rates inflation"
    for d in ("2026-06-01", "2026-07-05", "2026-08-01", "2026-09-01", "2026-10-01"):
        cutoff = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
        raw = get_json(gdelt.URL, {"query": q, "mode": "artlist", "format": "json", "sort": "datedesc",
                                   "maxrecords": 50, "startdatetime": cutoff.replace(day=1).strftime("%Y%m%d%H%M%S")
                                   if cutoff.day > 1 else (cutoff.replace(month=cutoff.month - 1)).strftime("%Y%m%d%H%M%S"),
                                   "enddatetime": cutoff.strftime("%Y%m%d%H%M%S")})
        arts = (raw or {}).get("articles", []) or []
        seen = sorted(a.get("seendate", "") for a in arts)
        after = sum(1 for s in seen if s >= cutoff.strftime("%Y%m%dT%H%M%SZ"))
        print(f"отсечка {d}: статей {len(arts)}, диапазон {seen[0] if seen else '—'} … {seen[-1] if seen else '—'}, "
              f"после отсечки (утечка у сервера) {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
