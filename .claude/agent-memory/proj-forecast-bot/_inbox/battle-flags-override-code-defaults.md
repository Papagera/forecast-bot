---
name: battle-flags-override-code-defaults
description: Боевой режим бота задают флаги в .github/workflows/forecast.yml, а не дефолты в коде — перед утверждением «бот делает X» читать workflow
metadata:
  type: feedback
---

07.10.2026 я обосновал PR #17 (журнал разброса прогнозов) фразой «бот уже делает FORECAST_PREDICTIONS=5» по дефолту
в `forecast_bot/bot.py:106`. На деле боевой workflow передаёт `--predictions 1` (`.github/workflows/forecast.yml:79`):
после выкатки в журнале `predictions_n = 1`, разброса нет. Ошибка всплыла только на проверке после выкатки.

**Why:** дефолт в коде описывает локальный/тестовый запуск; бой собирают флаги CLI в workflow (модель, reasoning,
число прогнозов, бюджет). Утверждение о бое по коду — та самая «реконструкция по имени», которую запрещает §7.

**How to apply:** любое «бот в бою делает …» — сначала `grep` по `.github/workflows/forecast.yml` (строка запуска
`forecast_bot.run`), цитировать `файл:строка` оттуда. В риск-листе правки боевого кода — отдельной строкой «как это
запускается в Actions».
