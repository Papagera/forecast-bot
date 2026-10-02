# Откуда взят код в vendor/

| Папка | Источник | Версия | Скопировано | Правки |
|---|---|---|---|---|
| `metac_bot_template/` | github.com/Metaculus/metac-bot-template | коммит `da5de87fcf` (main, 01.10.2026) | 02.10.2026 | только шапка-комментарий «КОПИЯ» в `.py` |
| `forecasting-tools-0.3.2/` | PyPI `forecasting-tools==0.3.2` (sdist, выложен 01.10.2026) = github.com/Metaculus/forecasting-tools | 0.3.2 | 02.10.2026 | нет |

Почему 0.3.2, а не версия из `poetry.lock` шаблона (0.2.92 от 27.05.2026): в 0.2.92
`CURRENT_AI_COMPETITION_ID = FE_SUMMER_2026_ID` (летний турнир 33022), в 0.3.2 — `FE_FALL_2026_ID = 33121`
(`forecasting_tools/helpers/metaculus_client.py`). `pyproject.toml` шаблона допускает `>=0.2.90,<0.4.0`.

Обновлять — новой копией целиком + прогон `pytest` и `tools/mutate_guards.py`: сторож в
`forecast_bot/guarded_llm.py` держится за `general_llm.acompletion/aresponses` и
`GeneralLlm._mockable_direct_call_to_model` — при переименовании в новой версии тесты покраснеют.
