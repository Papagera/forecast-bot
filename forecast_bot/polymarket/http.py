"""Единственный HTTP-выход модуля Polymarket: только GET, только белый список хостов, вежливый темп."""
from __future__ import annotations

import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests

ALLOWED_HOSTS = {"gamma-api.polymarket.com", "clob.polymarket.com", "api.gdeltproject.org"}
# GDELT: «Please limit requests to one every 5 seconds» (ответ API 05.10.2026) — держим 6 с.
MIN_INTERVAL_S = {"api.gdeltproject.org": 6.0, "gamma-api.polymarket.com": 0.3, "clob.polymarket.com": 0.3}
UA = {"User-Agent": "forecast-bot research (read-only)"}

_last: dict[str, float] = {}
_lock = threading.Lock()


class ForbiddenRequest(RuntimeError):
    """Запрос вне белого списка или не GET — отклонён до сети."""


def _wait(host: str, sleep=time.sleep, clock=time.time) -> None:
    gap = MIN_INTERVAL_S.get(host, 0.5)
    with _lock:
        wait = _last.get(host, 0.0) + gap - clock()
        if wait > 0:
            sleep(wait)
        _last[host] = clock()


def get_json(url: str, params: Optional[dict] = None, *, retries: int = 3, timeout: int = 40) -> Any:
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise ForbiddenRequest(f"хост не в белом списке: {host}")
    last_exc: Exception | None = None
    for attempt in range(retries):
        _wait(host)
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 429 or (host == "api.gdeltproject.org" and r.text.startswith("Please limit")):
                time.sleep(MIN_INTERVAL_S.get(host, 1.0) * (attempt + 2))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {host} не удался после {retries} попыток: {last_exc}")
