# CLAUDE.md — forecast-bot

Личный бот-прогнозист для ботовых турниров Metaculus (FutureEval Fall 2026, MiniBench): шаблон metac-bot-template, поиск новостей, ансамбль моделей, запуск по расписанию. Личное, вне EZCAR

## Границы
Правим ТОЛЬКО внутри этой папки. Чужое — копировать к себе, не править (§3).

## Порт
Не назначен и не нужен: сетевого сервиса нет (бот — короткий прогон по расписанию).

## Устройство (этап 1, ТЗ `docs/ТЗ-этап1-2026-10-02.md`)
- `vendor/metac_bot_template/` — копия шаблона @da5de87 (не правится, поведение — подклассом).
- `vendor/forecasting-tools-0.3.2/` — копия библиотеки (sdist PyPI; в `poetry.lock` шаблона устаревшая
  0.2.92, она ещё целилась в летний турнир 33022). Ставится `pip install --no-deps -e`.
- `forecast_bot/bot.py` — подкласс `FallTemplateBot2026`: все роли LLM = `GuardedLlm`, сам НЕ публикует.
- `forecast_bot/guarded_llm.py` — каждый вызов модели через `ai_guard.acall` + сторож: `acompletion`/
  `aresponses` в forecasting-tools подменены обёрткой, которая отказывает вызову мимо гарда.
- `forecast_bot/ai_guard.py` — копия общего гарда (из clipper) с аддитивными правками в шапке.
  Леджер общий: `~/.ezcar/ai_ledger.db`, строки бота — `user = 'forecast:q<id вопроса>'`.
- `forecast_bot/run.py` — раннер: вопросы по одному, журнал, отправка, отчёт dry-run.
- `forecast_bot/journal.py` — SQLite `data/journal.db` (основной чекаут, вне git).

## Инварианты (каждый держит тест + мутация в `tools/mutate_guards.py`)
- Отправка — только `--mode submit` **и** `FORECAST_SUBMIT=1` в `.env`. По умолчанию dry.
- Не дважды: флаг Metaculus `already_forecasted` + журнал (`mode='submit' AND status='ok'`); dry-строки не блокируют.
- Деньги: `--run-budget` (Actions: $1 за запуск) + `APP_LIMITS["forecast"]` $6/сутки по леджеру
  (с 03.10.2026; замеры вариантов — отдельное приложение `forecast-lab`, $8/сутки, `FORECAST_APP`).
- Агент (`--research agent`): каждое число справки сверяется с процитированным источником `[S#]`
  (`forecast_bot/verify.py`); факт с непроверенным числом до прогнозиста не доходит.
- Любой отказ ai_guard по бюджету → `skipped_budget`, ничего не отправлено, прогон стоп, код 0.
  RPM/TPM — не бюджет: вызов ждёт окно 61 с (до 5 раз).
- Вызовов через гард ≠ строк в леджере → вопрос `error`, отправки нет.
- AskNews: 6 вызовов на вопрос (свежие 1 + архив 5), потолок 900/мес → `skipped_asknews_quota`.
- `.env`, журнал, отчёты — в ОСНОВНОМ чекауте (`paths.main_checkout()`), не в рабочем дереве.

## Запуск
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt \
  && .venv/bin/pip install --no-deps -e vendor/forecasting-tools-0.3.2
.venv/bin/python -m forecast_bot.run --mode dry --tournament minibench --limit 5   # dry-run
.venv/bin/python -m forecast_bot.run --mode submit --tournament both               # боевой (после «да»)
```
Бой — GitHub Actions `.github/workflows/forecast.yml` (репо публичный → минуты не из квоты Papagera,
решение Никиты 02.10.2026): каждые 20 мин (окно приёма прогноза — 3 ч) + `workflow_dispatch`.
Job идёт только при переменной репо `FORECAST_SUBMIT=1`; выключить бота — удалить переменную.
Секреты: `METACULUS_TOKEN`, `OPENROUTER_API_KEY`, `ASKNEWS_API_KEY`. Журнал и леджер между запусками —
`actions/cache` (`state/`); потеря кэша не ведёт к дублям: первичный признак — `my_forecasts` по API.
LaunchAgent на маке не используется.

## Тесты
`.venv/bin/python -m pytest -q` — офлайн (сеть в тестах закрыта, LLM/AskNews/Metaculus — фейки).
Мутации: `.venv/bin/python tools/mutate_guards.py` — все должны быть КРАСНЫМИ.
pre-push ставится: `bash ~/Desktop/КЛОД/install-hooks.sh`

## Правила
Действуют общие: `EZCAR_SHARED_RULES.md` (изоляция §3, согласование правок §3a,
git §4, суточная выкатка §4c, ai_guard §4b, источники цифр §7).
