"""Перезапуск боевого цикла в конце job (`gh workflow run forecast.yml`) — с повторами на сбоях GitHub.

07.10.2026 16:53Z `gh workflow run` получил «HTTP 500» (инцидент GitHub Actions 15:14Z/17:17Z по githubstatus.com),
шаг упал, цепочка оборвалась; cron-прогон того же часа GitHub закрыл без единого job — бот не работал час.
Теперь: до 5 повторов с паузами 30/60/120/240/300 с на 5xx, 429 и сетевых ошибках; прочие 4xx (права, ref) — сразу
провал: повтор не поможет. Не вышло — шаг падает (видно в Actions), подхватывает cron раз в час.

    python -m forecast_bot.restart --repo owner/name --ref main
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from typing import Callable

PAUSES_S = (30, 60, 120, 240, 300)
MAX_WAIT_S = sum(PAUSES_S)   # закладывается в timeout-minutes job (тест workflow)
_RETRYABLE = re.compile(r"HTTP 5\d\d|HTTP 429|timed? ?out|timeout|connection (reset|refused)|EOF|"
                        r"could not resolve|TLS handshake|temporary failure|Bad Gateway|Service Unavailable", re.I)


def retryable(stderr: str) -> bool:
    """5xx, 429 (лимит запросов GitHub) и сетевые ошибки — повторяем; остальные 4xx и прочее — нет."""
    return bool(_RETRYABLE.search(stderr or ""))


def dispatch(repo: str, ref: str) -> tuple[int, str]:
    p = subprocess.run(["gh", "workflow", "run", "forecast.yml", "--repo", repo, "--ref", ref],
                       capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stderr or "") + (p.stdout or "")


def restart(repo: str, ref: str, run: Callable[[str, str], tuple[int, str]] = dispatch,
            sleep: Callable[[float], None] = time.sleep, log=print) -> int:
    for attempt in range(len(PAUSES_S) + 1):
        try:
            code, out = run(repo, ref)
        except subprocess.TimeoutExpired:
            code, out = 1, "timeout"
        if code == 0:
            log(f"цикл перезапущен (попытка {attempt + 1})")
            return 0
        log(f"перезапуск, попытка {attempt + 1}: {out.strip()[:200]}")
        if not retryable(out):
            log("ошибка не временная — без повторов")
            return 1
        if attempt < len(PAUSES_S):
            sleep(PAUSES_S[attempt])
    log(f"перезапуск не удался за {len(PAUSES_S) + 1} попыток — подхватит cron (раз в час)")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", required=True)
    a = ap.parse_args(argv)
    return restart(a.repo, a.ref)


if __name__ == "__main__":
    sys.exit(main())
