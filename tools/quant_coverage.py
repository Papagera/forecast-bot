"""Какие «рыночные» вопросы каталога quant не разобрал — для доводки разбора. Только чтение."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forecast_bot import paths, quant as Q  # noqa: E402

MARKET = r"yahoo finance|clos|stock price|share price|nasdaq|s&p 500|bitcoin|ethereum|xrp|solana|exchange rate|usd/|/usd|dow jones|vix"


def main() -> int:
    rows = [json.loads(l) for l in (paths.data_dir() / "polygon" / "minibench.jsonl").open()]
    miss, ok = [], 0
    for r in rows:
        if not re.search(MARKET, r["title"], re.I):
            continue
        y = datetime.fromisoformat(r["open_time"].replace("Z", "+00:00")).year
        if Q.parse(r["title"], r["type"], y):
            ok += 1
        else:
            miss.append(f"[{r['type']}] {r['title']}")
    print(f"разобрано {ok}, не разобрано {len(miss)}")
    print("\n".join(miss))
    return 0


if __name__ == "__main__":
    sys.exit(main())
