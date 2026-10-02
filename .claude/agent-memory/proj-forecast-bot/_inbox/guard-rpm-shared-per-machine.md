---
name: guard-rpm-shared-per-machine
description: ai_guard RPM 30 общий на машину — бот с ~16 вызовами на вопрос упирается в него; RPM/TPM надо ждать, а не считать бюджетом
metadata:
  type: project
---

RPM 30 / TPM 150k в ai_guard считаются по ВСЕМУ леджеру машины (`~/.ezcar/ai_ledger.db`), а один вопрос
Metaculus в шаблоне = ~16 LLM-вызовов (1 сводка + 5 прогнозов + 10 парсеров). На быстрых ответах третий
вопрос подряд получает `BudgetExceeded: RPM 30` — это темп, а не деньги.

**Why:** 02.10.2026 офлайн-тест на 4 вопросах дал `['ok','ok','skipped_budget']` — вопрос «пропущен по
бюджету», хотя денег было с запасом. В проде так терялись бы вопросы в каждом прогоне.

**How to apply:** в `GuardedLlm` отказ гарда с текстом `RPM `/`TPM ` → ждать 61 с и повторить (до 5 раз);
любой другой `BudgetExceeded` — честный пропуск без повтора. Держит тест
`test_rpm_waits_for_window_instead_of_failing` и мутация «RPM валит вопрос». Класс общий для всех
потребителей ai_guard с пачками вызовов (clipper, content-factory) — кандидат в `_inbox` владельца ai_guard.
