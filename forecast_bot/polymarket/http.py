"""Единственный HTTP-выход модуля Polymarket: только GET, только белый список хостов, вежливый темп."""
from __future__ import annotations

import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests

ALLOWED_HOSTS = {"gamma-api.polymarket.com", "clob.polymarket.com", "api.gdeltproject.org",
                 "data.gdeltproject.org"}  # статические выгрузки GDELT (решение income 05.10.2026) — только GET
# GDELT: «Please limit requests to one every 5 seconds» (ответ API 05.10.2026); на практике после нарушений штраф
# дольше и продлевается повторами — держим 10 с между запросами.
MIN_INTERVAL_S = {"api.gdeltproject.org": 10.0, "data.gdeltproject.org": 0.2, "gamma-api.polymarket.com": 0.3, "clob.polymarket.com": 0.3}
UA = {"User-Agent": "forecast-bot research (read-only)"}
RATE_BACKOFF_S = {"api.gdeltproject.org": 120.0}

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


def _check(url: str) -> str:
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
        raise ForbiddenRequest(f"хост не в белом списке: {host}")
    return host


def get_bytes(url: str, *, retries: int = 3, timeout: int = 120) -> Optional[bytes]:
    """GET файла (выгрузки GDELT). 404 → None (такого 15-минутного файла нет), остальное — повтор."""
    host = _check(url)
    last_exc: Exception | None = None
    for attempt in range(retries):
        _wait(host)
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(3.0 * (attempt + 1))
    raise RuntimeError(f"GET {host} не удался после {retries} попыток: {last_exc}")


def get_json(url: str, params: Optional[dict] = None, *, retries: int = 4, timeout: int = 40) -> Any:
    host = _check(url)
    last_exc: Exception | None = None
    for attempt in range(retries):
        _wait(host)
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 429 or (host == "api.gdeltproject.org" and r.text.startswith("Please limit")):
                # GDELT после частых запросов отказывает дольше заявленных 5 с (живьём 05.10: одиночный запрос
                # прошёл только через ~75 с паузы, серия с отступом 30 с не прошла) — отступ 120 с × попытку.
                last_exc = RuntimeError(f"{host}: rate limited")
                time.sleep(RATE_BACKOFF_S.get(host, 5.0) * (attempt + 1))
                continue
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"GET {host}: {r.status_code} {r.reason}")  # клиентская ошибка — без повторов
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {host} не удался после {retries} попыток: {last_exc}")
