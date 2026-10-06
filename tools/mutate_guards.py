"""Мутационная проверка гардов (§7a): ломаем предохранитель в памяти → его тест ОБЯЗАН покраснеть.

    .venv/bin/python tools/mutate_guards.py

Каждая мутация — в отдельном процессе: подмена исходника модуля (replace по строке) или атрибута,
затем pytest по целевому тесту. Зелёный тест на сломанном коде = слепой гард = код выхода 1.
Если строка для подмены не найдена — тоже провал: мутация устарела и ничего не проверяет.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# имя → (модуль, старая строка, новая строка, целевой тест)
SOURCE_MUTATIONS = {
    "гейт отправки без env": (
        "forecast_bot.run", 'return mode == "submit" and env.get("FORECAST_SUBMIT", "").strip() == "1"',
        'return mode == "submit"', "tests/test_run.py::test_submit_requires_env_flag_too"),
    "main шлёт без флага": (
        "forecast_bot.run", 'if args.mode == "submit" and not submit:', "if False:",
        "tests/test_run.py::test_main_submit_without_flag_refuses_before_any_work"),
    "не дважды: журнал": (
        "forecast_bot.journal", "return row is not None", "return False",
        "tests/test_run.py::test_not_twice_second_run_posts_nothing"),
    "не дважды: флаг Metaculus": (
        "forecast_bot.run", "q.already_forecasted or ", "",
        "tests/test_run.py::test_already_forecasted_on_metaculus_is_skipped"),
    "dry-строка как отправка": (
        "forecast_bot.journal", "AND mode = 'submit' ", "",
        "tests/test_run.py::test_dry_row_does_not_block_real_submit"),
    "лимит гарда не ловится": (
        "forecast_bot.run", "budget_hit = guarded_llm.BUDGET_HITS.pop(user, None)", "budget_hit = None",
        "tests/test_run.py::test_budget_hit_mid_question_publishes_nothing"),
    "сторож обхода выключен": (
        "forecast_bot.guarded_llm", "if not _IN_GUARD.get():", "if False:",
        "tests/test_guard.py::test_sentinel_blocks_plain_generalllm"),
    "сверка с леджером выключена": (
        "forecast_bot.run", " or guarded_calls != ledger_calls", "",
        "tests/test_run.py::test_ledger_miss_blocks_submission"),
    "потолок AskNews снят": (
        "forecast_bot.run", "> asknews_cap:", "> 10**9:",
        "tests/test_run.py::test_asknews_monthly_cap_skips_without_llm"),
    "лимит повторяется": (
        # отказ гарда проваливается в общую ветку «сетевая ошибка → повтор»
        "forecast_bot.guarded_llm", "except BudgetExceeded as exc:", "except ZeroDivisionError as exc:",
        "tests/test_guard.py::test_budget_error_is_not_retried"),
    "фактическая цена игнорируется": (
        "forecast_bot.ai_guard", "if usage.actual_cost_usd is not None and usage.actual_cost_usd > 0:", "if False:",
        "tests/test_guard.py::test_actual_cost_from_provider_wins_over_price_table"),
    "счёт OpenRouter игнорируется": (
        "forecast_bot.guarded_llm", "billed = [c for c in sink if c is not None]", "billed = []",
        "tests/test_guard.py::test_provider_billed_cost_goes_to_ledger"),
    "режим поиска не доходит": (
        "forecast_bot.bot", '"researcher": build_researcher(research, model),', '"researcher": ASKNEWS_PRESET,',
        "tests/test_run.py::test_research_modes_without_asknews"),
    "свежие новости считаются как архив": (
        "forecast_bot.bot", "ASKNEWS_LATEST: 1}", "ASKNEWS_LATEST: 0}",
        "tests/test_run.py::test_asknews_latest_mode_costs_one_call"),
    "лимит запуска не проверяется": (
        "forecast_bot.guarded_llm", "        _check_run_budget()\n", "",
        "tests/test_run.py::test_run_budget_stops_run_and_publishes_nothing_partial"),
    "дневной потолок бота снят": (
        "forecast_bot.ai_guard", '"forecast": {"day_usd": 6.0},', '"forecast": {"day_usd": 1e9},',
        "tests/test_run.py::test_daily_cap_counts_earlier_runs_from_saved_ledger"),
    "агент: новости без лимита": (
        "forecast_bot.agent", "if self.news is None or self.news_failed or self.news_calls >= self.max_news:",
        "if self.news is None or self.news_failed:",
        "tests/test_agent.py::test_news_calls_capped_per_question"),
    "агент: шаги без лимита": (
        "forecast_bot.agent", "last = step == MAX_STEPS or over_budget", "last = over_budget",
        "tests/test_agent.py::test_steps_capped_and_last_step_has_no_tool_use"),
    "агент: нет мягкого стопа по деньгам": (
        "forecast_bot.agent", "over_budget = self._spent(t0) >= SOFT_STOP_SHARE * self.question_budget_usd",
        "over_budget = False", "tests/test_agent.py::test_soft_budget_stop_forces_final_brief"),
    "агент: ходит в локальную сеть": (
        "forecast_bot.agent", "if ip.is_private or ip.is_loopback", "if False and ip.is_private or ip.is_loopback and False",
        "tests/test_agent.py::test_fetch_url_refuses_non_public"),
    "агент: мимо гарда": (
        "forecast_bot.agent", "resp = await guarded_llm.guarded_completion(self.model, messages, max_tokens=self.max_tokens,",
        "from forecasting_tools.ai_models import general_llm as _g; resp = await _g.acompletion(model=self.model, messages=messages, max_tokens=self.max_tokens,",
        "tests/test_agent.py::test_agent_research_reaches_forecaster_and_ledger"),
    "сверка: выдуманное число проходит": (
        "forecast_bot.verify", "        if missing:\n            unverified += missing", "        if False:\n            unverified += missing",
        "tests/test_verify.py::test_invented_number_drops_whole_fact"),
    "сверка: любой источник вместо процитированного": (
        "forecast_bot.verify", 'ids = (cited_ids(line) & set(norm)) | {"S0"}', 'ids = set(norm)',
        "tests/test_verify.py::test_number_from_wrong_source_is_dropped"),
    "сверка: частичное совпадение числа": (
        "forecast_bot.verify", 'rf"(?<![\\d.]){re.escape(num)}(?:0*)(?![\\d])"', 'rf"{re.escape(num)}"',
        "tests/test_verify.py::test_partial_match_is_not_a_match"),
    "агент: сверка не применяется": (
        "forecast_bot.agent", "        return self.verdict.text\n", "        return self.raw_brief\n",
        "tests/test_verify.py::test_agent_hallucination_is_dropped_and_journaled"),
    "приложение без потолка разрешено": (
        "forecast_bot.guarded_llm", "if app not in ai_guard.APP_LIMITS:", "if False:",
        "tests/test_guard.py::test_unknown_app_name_refused"),
    "цикл: лимит опроса не обнуляется": (
        "forecast_bot.run", "            guarded_llm.start_run(run_budget)\n            stats.polls += 1",
        "            stats.polls += 1", "tests/test_loop.py::test_run_budget_reset_on_every_poll"),
    "цикл: суточный потолок не проверяется": (
        "forecast_bot.run", "if cap is not None and day_spent() >= cap:", "if False:",
        "tests/test_loop.py::test_day_cap_skips_polls_without_touching_metaculus"),
    "цикл: ошибка опроса роняет цикл": (
        "forecast_bot.run", "except Exception as exc:  # сеть/API", "except ZeroDivisionError as exc:  # сеть/API",
        "tests/test_loop.py::test_exception_in_poll_does_not_stop_loop"),
    "цикл: выходит за окно": (
        "forecast_bot.run", "if clock() + poll_s >= deadline:", "if clock() >= deadline + 10 * poll_s:",
        "tests/test_loop.py::test_polls_every_interval_until_window_ends"),
    "цикл: новые вопросы после срока": (
        "forecast_bot.run", "if stop_at is not None and time.time() > stop_at:", "if False:",
        "tests/test_loop.py::test_stop_at_does_not_start_new_questions"),
    "pulse: обновление без паузы": (
        "forecast_bot.run", "    return age >= UPDATE_EVERY_S", "    return True",
        "tests/test_pulse.py::test_needs_update"),
    "pulse: нет финального обновления перед закрытием": (
        "forecast_bot.run", "    if close_ts is not None and close_ts - now <= FINAL_WINDOW_S:", "    if False:",
        "tests/test_pulse.py::test_needs_update"),
    "pulse: обновления не работают («не дважды» везде)": (
        "forecast_bot.run", "            if tournament in refresh:\n                # Spot-очки",
        "            if False:\n                # Spot-очки",
        "tests/test_pulse.py::test_group_subquestions_forecast_and_refresh_on_schedule"),
    "pulse: «не дважды» снят и для обычных турниров": (
        "forecast_bot.run", "            elif submit and (q.already_forecasted or journal.already_submitted(qid)):",
        "            elif False:", "tests/test_pulse.py::test_non_spot_tournament_keeps_not_twice"),
    "pulse: несуществующий сезон роняет прогон": (
        "forecast_bot.run", "            if tournament in refresh:  # сезона Market Pulse", "            if False:  # сезона Market Pulse",
        "tests/test_pulse.py::test_missing_pulse_season_is_skipped_quietly"),
    "quant: подсказка для начавшегося периода": (
        "forecast_bot.quant", "if spec is None or per is None or per[0] <= asof:", "if spec is None or per is None:",
        "tests/test_pulse.py::test_pulse_quant_percentiles_and_no_lookahead"),
    "quant: подсказка без флага": (
        "forecast_bot.bot", 'if os.environ.get("FORECAST_QUANT_HINTS", "").strip() != "1":', "if False:",
        "tests/test_pulse.py::test_quant_hint_reaches_forecaster_only_with_flag"),
    "pulse: поиск на каждое обновление ряда": (
        "forecast_bot.bot", '            return hint + "\\n\\nNo news search was run for this market-series question."',
        '            return hint + "\\n\\n" + await self._run_research_inner(question)',
        "tests/test_pulse.py::test_market_series_question_skips_search"),
    "MC: буква сдвинута на позицию": (
        "forecast_bot.mc", 'k = ord(lm.group(1).lower()) - ord("a")', 'k = ord(lm.group(1).lower()) - ord("a") + 1',
        "tests/test_mc.py::test_placeholders_map_by_position_real_house_case"),
    "MC: берётся первый блок, а не финальный": (
        "forecast_bot.mc", "for block, pct in reversed(blocks):", "for block, pct in blocks:",
        "tests/test_mc.py::test_last_complete_block_wins"),
    "MC: промпт с заглушками Option_A": (
        "forecast_bot.bot", "mc.patch_prompt(prompt, list(question.options))", "prompt",
        "tests/test_mc.py::test_bot_sends_explicit_names_and_parses_without_llm_parser"),
    "MC: снова гадает LLM-парсер": (
        "forecast_bot.bot", "        if parsed is not None:\n            options = PredictedOptionList(",
        "        if False:\n            options = PredictedOptionList(",
        "tests/test_mc.py::test_bot_sends_explicit_names_and_parses_without_llm_parser"),
    "агент: котировки снова со Stooq": (
        "forecast_bot.agent", 'f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"', '"https://stooq.com/q/d/l/"',
        "tests/test_agent.py::test_stock_history_reads_yahoo"),
    "pulse: несуществующий сезон всё равно запрашивается": (
        "forecast_bot.run", "and not tournament_exists(tournament):", "and False:",
        "tests/test_pulse.py::test_missing_season_is_not_fetched"),
    "pulse: отказ «нет турнира» не кэшируется": (
        "forecast_bot.run", "if hit and (hit[0] or now - hit[1] < MISSING_TTL_S):", "if hit and hit[0]:",
        "tests/test_pulse.py::test_tournament_exists_caches_missing_for_an_hour"),
    "модель боя без цены в гарде": (
        "forecast_bot.ai_guard", '    ("openrouter", "openrouter/google/gemini-3.8-flash"): (0.00075, 0.00375),', "",
        "tests/test_run.py::test_workflow_models_have_prices"),
    "AskNews упал → агент роняет вопрос": (
        "forecast_bot.agent", "        try:\n            return await self._run_tool_inner(name, args)\n        except Exception as exc:",
        "        try:\n            return await self._run_tool_inner(name, args)\n        except ZeroDivisionError as exc:",
        "tests/test_agent.py::test_agent_survives_asknews_wallet_empty"),
    "AskNews упал → режим «свежие» роняет вопрос": (
        "forecast_bot.bot", "        except Exception as exc:\n            # Поиск упал",
        "        except ZeroDivisionError as exc:\n            # Поиск упал",
        "tests/test_agent.py::test_asknews_latest_mode_survives_wallet_empty"),
    "отказ гарда глотается как сбой поиска": (
        "forecast_bot.bot", "        except (BudgetExceeded, UnguardedLlmCall):\n            raise",
        "        except ZeroDivisionError:\n            raise",
        "tests/test_agent.py::test_guard_refusal_in_research_is_not_swallowed"),
    "polymarket: цена в момент t (утечка)": (
        "forecast_bot.polymarket.markets", "prev = [p for t, p in self.history if t < ts]",
        "prev = [p for t, p in self.history if t <= ts]", "tests/test_polymarket.py::test_price_before_is_strict"),
    "polymarket: GDELT без отсечки на клиенте": (
        "forecast_bot.polymarket.gdelt", "return [a for a in arts if a.seen < cutoff]", "return list(arts)",
        "tests/test_polymarket.py::test_gdelt_cutoff_is_enforced_client_side"),
    "polymarket: любой хост": (
        "forecast_bot.polymarket.http", 'if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:', "if False:",
        "tests/test_polymarket.py::test_http_allows_only_get_to_whitelist"),
    "polymarket: рынки дольше 30 дней": (
        "forecast_bot.polymarket.markets", "if life > max_life_days or life < MIN_LIFE_DAYS:", "if False:",
        "tests/test_polymarket.py::test_from_gamma_rejects"),
    "polymarket: сделка без порога": (
        "forecast_bot.polymarket.backtest", "    if abs(edge) < thr:\n        return None", "    if False:\n        return None",
        "tests/test_polymarket.py::test_paper_trade_threshold_side_fee_and_pnl"),
    "polymarket: без комиссии": (
        "forecast_bot.polymarket.backtest", "fee = fee_rate * c * (1 - c)", "fee = 0.0",
        "tests/test_polymarket.py::test_paper_trade_threshold_side_fee_and_pnl"),
    "polymarket: потолок этапа не считается": (
        "forecast_bot.polymarket.backtest", "return STAGE_CAP_USD - stage_spent()", "return STAGE_CAP_USD",
        "tests/test_polymarket.py::test_stage_cap_from_ledger"),
    "polymarket: дубль точки t50 = t48": (
        "forecast_bot.polymarket.backtest", 'if "t50" in out and abs(out["t50"] - t48) < timedelta(hours=12):', "if False:",
        "tests/test_polymarket.py::test_points_respect_lifetime"),
    "polymarket: отчёт считает дубли точек": (
        "forecast_bot.polymarket.backtest",
        'if r["point"] == "t50" and k in t48 and abs(datetime.fromisoformat(r["t"]) - t48[k]) < timedelta(hours=12):',
        "if False:", "tests/test_polymarket.py::test_report_dedupes_coinciding_points"),
    "polymarket: GDELT конец окна без запаса на утечку сервера": (
        "forecast_bot.polymarket.gdelt", '"enddatetime": (cutoff - SERVER_LEAK)', '"enddatetime": (cutoff)',
        "tests/test_polymarket.py::test_gdelt_search_filters_fake_leaky_response"),
    "polymarket: режим gdelt без кэша идёт без новостей": (
        "tools.polymarket_backtest", "                if key not in cache:\n                    stats[\"нет GDELT в кэше\"] += 1\n                    continue\n                research = gdelt.as_research(cache[key])",
        "                research = gdelt.as_research(cache.get(key, []))",
        "tests/test_polymarket.py::test_gdelt_mode_forecasts_only_from_cache"),
    "polymarket: выгрузки GDELT — строка после t проходит": (
        "forecast_bot.polymarket.gdelt_files", "if art.seen >= t:", "if False:",
        "tests/test_polymarket.py::test_gdelt_files_rows_after_t_dropped_and_titles_matched"),
    "polymarket: выгрузки GDELT — файл без запаса до t": (
        "forecast_bot.polymarket.gdelt_files", "SAFETY = timedelta(hours=1)", "SAFETY = timedelta(hours=-1)",
        "tests/test_polymarket.py::test_gdelt_files_never_after_t"),
    "polymarket: data.gdeltproject.org — не https": (
        "forecast_bot.polymarket.http", 'if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:',
        "if host not in ALLOWED_HOSTS:", "tests/test_polymarket.py::test_http_allows_only_get_to_whitelist"),
    "поиск: нет отдельной строки в леджере": (
        "forecast_bot.guarded_llm", "            if search_cost:\n", "            if False:\n",
        "tests/test_websearch.py::test_search_writes_two_ledger_rows_and_returns_sources"),
    "поиск: цена по прайсу вместо факта": (
        "forecast_bot.guarded_llm", "if total is not None and upstream is not None and total >= upstream:", "if False:",
        "tests/test_websearch.py::test_split_search_cost_prefers_fact_over_price"),
    "поиск: строка леджера не учтена сторожем": (
        "forecast_bot.guarded_llm", "                GUARDED_CALLS[user] += 1  # строка леджера = учтённый вызов",
        "                pass  # строка леджера = учтённый вызов",
        "tests/test_websearch.py::test_agent_with_web_search_end_to_end"),
    "AskNews разрешён в замерах": (
        "forecast_bot.bot", '        if guarded_llm.APP != "forecast":', "        if False:",
        "tests/test_websearch.py::test_asknews_forbidden_outside_battle"),
    "по умолчанию снова AskNews": (
        "forecast_bot.bot", 'return os.environ.get("FORECAST_SEARCH", "web").strip() or "web"',
        'return os.environ.get("FORECAST_SEARCH", "asknews").strip() or "asknews"',
        "tests/test_websearch.py::test_web_is_default_search_and_asknews_needs_flag"),
    "402: пустой кошелёк не останавливает опрос": (
        "forecast_bot.run", 'if is_credit_error(row["error"]):', "if False:",
        "tests/test_credits.py::test_credit_error_stops_poll_loudly_and_sends_nothing"),
    "402: строка цикла молчит про ошибки вопросов": (
        "forecast_bot.run", 'stats.question_errors += res.count("error")', "pass",
        "tests/test_credits.py::test_loop_line_shows_question_errors_and_credit_alarm"),
    "402: итог запуска без красной строки": (
        "forecast_bot.summary", 'credit = sum(1 for r in errors if is_credit_error(r["error"] or ""))', "credit = 0",
        "tests/test_credits.py::test_summary_flags_credit_errors"),
    "pm2: бары из будущего видны модели": (
        "forecast_bot.polymarket.series_data", 'n = int(np.searchsorted(self.avail, t, side="right"))',
        "n = len(self.avail)", "tests/test_polymarket_series.py::test_price_dist_ignores_future_bars"),
    "pm2: точки от closedTime, а не плановой endDate": (
        "forecast_bot.polymarket.series", "out = B.points(m.start, m.end_planned)", "out = B.points(m.start, m.closed)",
        "tests/test_polymarket_series.py::test_points_from_planned_end_not_close_time"),
    "pm2: рынок, закрытый до t, остаётся": (
        "forecast_bot.polymarket.series", "return {p: t for p, t in out.items() if m.closed > t}", "return out",
        "tests/test_polymarket_series.py::test_points_from_planned_end_not_close_time"),
    "pm2: порог пройден до t — точка не отсекается": (
        "forecast_bot.polymarket.series_quant", "return bool(seen.high.max() >= spec.lo)", "return False",
        "tests/test_polymarket_series.py::test_already_hit_drops_point"),
    "pm2: макро — опубликованное на t значение не отсекается": (
        "forecast_bot.polymarket.series_quant", "    if target in have:\n        return None, ",
        "    if False:\n        return None, ",
        "tests/test_polymarket_series.py::test_macro_uses_vintage_before_t_and_refuses_published_month"),
    "калибровка: прогнозы до агрегации не сохраняются": (
        "forecast_bot.bot", "        self.prediction_sets[question.id_of_question] = list(predictions)", "        pass",
        "tests/test_calib.py::test_all_predictions_and_spread_reach_journal_median_unchanged"),
    "калибровка: разброс не пишется в журнал": (
        "forecast_bot.run", "                base.update(calib.summarize(sets))", "                pass",
        "tests/test_calib.py::test_all_predictions_and_spread_reach_journal_median_unchanged"),
    "калибровка: ссылка на вопрос [S0] считается фактом": (
        "forecast_bot.verify", 'ids = (cited_ids(line) & set(norm)) - {"S0"}', "ids = cited_ids(line) & set(norm)",
        "tests/test_calib.py::test_verify_counts_cited_facts_and_sources"),
    "калибровка: любой сайт — официальный": (
        "forecast_bot.calib", "        return bool(host and OFFICIAL_HOST.search(host))", "        return bool(host)",
        "tests/test_calib.py::test_is_official"),
    "pm2: винтаж ALFRED на день t": (
        "forecast_bot.polymarket.series_quant", "return (t - timedelta(days=1)).date()", "return t.date()",
        "tests/test_polymarket_series.py::test_vintage_is_day_before_t"),
    "pm2: потолок этапа считает траты 3A": (
        "forecast_bot.polymarket.series_backtest", 'ai_guard.app_cost_since(f"{B.APP}:{LEDGER_PREFIX}"',
        "ai_guard.app_cost_since(B.APP", "tests/test_polymarket_series.py::test_stage2_budget_counts_only_pm2"),
    "pm2: потолок этапа не проверяется": (
        "tools.polymarket_series", "        if SB.stage_budget_left() <= 0:", "        if False:",
        "tests/test_polymarket_series.py::test_llm_stops_at_stage_cap"),
    "pm2: приложение гарда не проверено": (
        "tools.polymarket_series", "    if guarded_llm.APP != B.APP:", "    if False:",
        "tests/test_polymarket_series.py::test_llm_refuses_wrong_app"),
    "pm2: ответ LLM без JSON → нулевая поправка": (
        "forecast_bot.polymarket.series_llm", '        raise BadAnswer("в ответе нет JSON")',
        '        return 0.0, 1.0, ""', "tests/test_polymarket_series.py::test_llm_bad_answer_is_error_not_zero_shift"),
    "pm2: поправка LLM без рамок": (
        "forecast_bot.polymarket.series_llm", "    shift = max(-SHIFT_MAX, min(SHIFT_MAX, shift))", "    pass",
        "tests/test_polymarket_series.py::test_parse_answer_clips_and_rejects"),
    "pm2: новости после t из кэша проходят": (
        "tools.polymarket_series", 'for x in r["articles"] if datetime.fromisoformat(x["seen"]) < t]',
        'for x in r["articles"]]', "tests/test_polymarket_series.py::test_load_news_drops_articles_at_or_after_t"),
    "pm2: новости позже цены рынка в промпте": (
        "tools.polymarket_series", "arts = [x for x in news[news_key] if x.seen < t_info]",
        "arts = list(news[news_key])",
        "tests/test_polymarket_series.py::test_llm_through_guard_without_market_price_and_future_news"),
    "pm2: бутстрэп по строкам, а не кластерам": (
        "forecast_bot.polymarket.series_backtest", 'by[r["cluster"]].append(r)', "by[id(r)].append(r)",
        "tests/test_polymarket_series.py::test_bootstrap_resamples_clusters_not_rows"),
    "pm2: «микро» торгуется": (
        "forecast_bot.polymarket.series_backtest", '        if seg == "micro":', "        if False:",
        "tests/test_polymarket_series.py::test_trades_skip_micro_and_pay_fee"),
    "pm2: «микро» в сравнении Brier": (
        "forecast_bot.polymarket.series_backtest", "    rows = [r for r in rows if tradable(r)]", "    rows = rows",
        "tests/test_polymarket_series.py::test_report_excludes_micro_from_comparison"),
    "pm2: quant видит ряд до t, а не до цены рынка": (
        "tools.polymarket_series", "                dist, why = dist_for(m, t_info, st, cache)",
        "                dist, why = dist_for(m, t, st, cache)",
        "tests/test_polymarket_series.py::test_quant_sees_series_only_up_to_market_price_time"),
    "pm3: пост в момент t и позже виден": (
        "forecast_bot.polymarket.tg_news", "for p in posts if lo <= p.date < t]", "for p in posts if lo <= p.date]",
        "tests/test_polymarket_ukraine.py::test_select_posts_strictly_before_t_and_window"),
    "pm3: группа из папки читается": (
        "forecast_bot.polymarket.tg_news", '        if getattr(ent, "broadcast", False):', "        if True:",
        "tests/test_polymarket_ukraine.py::test_fetch_reads_only_folder_channels"),
    "pm3: любая папка": (
        "forecast_bot.polymarket.tg_news", 'folder = next((f for f in filters if getattr(f, "id", None) == folder_id), None)',
        "folder = next(iter(filters), None)", "tests/test_polymarket_ukraine.py::test_fetch_reads_only_folder_channels"),
    "pm3: название канала в выжимке": (
        "forecast_bot.polymarket.tg_news", "channel {channel_no.get(p.channel, 0)}", "channel {p.channel}",
        "tests/test_polymarket_ukraine.py::test_as_research_has_channel_numbers_not_names"),
    "pm3: Telegram до t, а не до цены рынка": (
        "tools.polymarket_ukraine", "return T.as_research(T.select(posts, t_info, kw[m.event_id]), chans)",
        "return T.as_research(T.select(posts, t_info + timedelta(hours=2), kw[m.event_id]), chans)",
        "tests/test_polymarket_ukraine.py::test_forecast_modes_use_only_info_before_market_price"),
    "pm3: режим без данных идёт «без новостей»": (
        "tools.polymarket_ukraine", "        if key not in gd:\n            return None", "        if key not in gd:\n            return \"\"",
        "tests/test_polymarket_ukraine.py::test_forecast_modes_use_only_info_before_market_price"),
    "pm3: фактическое закрытие в вопросе": (
        "tools.polymarket_ukraine", "dataclasses.replace(m, closed=m.end_planned)", "m",
        "tests/test_polymarket_ukraine.py::test_forecast_modes_use_only_info_before_market_price"),
    "pm3: потолок этапа считает чужие траты": (
        "tools.polymarket_ukraine", 'ai_guard.app_cost_since(f"{B.APP}:{LEDGER_PREFIX}"', "ai_guard.app_cost_since(B.APP",
        "tests/test_polymarket_ukraine.py::test_stage3_budget_counts_only_pm3_and_stops"),
    "pm3: потолок этапа не проверяется": (
        "tools.polymarket_ukraine", "            if stage_budget_left() <= 0:\n                print(f\"потолок",
        "            if False:\n                print(f\"потолок", "tests/test_polymarket_ukraine.py::test_stage3_budget_counts_only_pm3_and_stops"),
    "pm3: срок жизни > 60 дней": (
        "tools.polymarket_ukraine", "    if life > MAX_LIFE_DAYS or life < M.MIN_LIFE_DAYS:\n        return None",
        "    if False:\n        return None", "tests/test_polymarket_ukraine.py::test_market_life_up_to_60_days_and_geopolitics_fee_zero"),
    "pm3: выборы в теме": (
        "tools.polymarket_ukraine", "and not NOT_WAR.search(title or \"\")", "", "tests/test_polymarket_ukraine.py::test_on_topic"),
    "pm3: режимы на разных точках": (
        "tools.polymarket_ukraine", "common = [v for v in by.values() if all(mo in v for mo in modes)]",
        "common = list(by.values())", "tests/test_polymarket_ukraine.py::test_report_compares_modes_on_common_points"),
    "RPM валит вопрос": (
        "forecast_bot.guarded_llm", "if _is_rate_limit(exc) and rate_waits < RATE_WAIT_TRIES:", "if False:",
        "tests/test_guard.py::test_rpm_waits_for_window_instead_of_failing"),
}

ATTR_MUTATIONS = {
    "данные в рабочем дереве": (
        "from forecast_bot import paths; paths.main_checkout = lambda root=paths.PACKAGE_ROOT: root",
        "tests/test_guard.py::test_paths_point_to_main_checkout"),
    "голый GeneralLlm вместо GuardedLlm": (
        "import forecast_bot.bot as b; from forecasting_tools import GeneralLlm; "
        "b.GuardedLlm = lambda model, max_tokens=None, **k: GeneralLlm(model, **k)",
        "tests/test_run.py::test_every_llm_call_lands_in_ledger"),
}

SRC_PATCH = (
    "import importlib, sys; m = importlib.import_module({mod!r}); src = open(m.__file__).read(); "
    "assert {old!r} in src, 'MUTATION_STALE'; "
    "exec(compile(src.replace({old!r}, {new!r}, 1), m.__file__, 'exec'), m.__dict__)"
)
RUN = ("import sys; sys.path.insert(0, {root!r}); {patch}; import pytest; "
       "sys.exit(pytest.main(['-q', '-x', '-p', 'no:cacheprovider', '-p', 'no:warnings', {target!r}]))")


def _check(name: str, patch: str, target: str) -> bool:
    proc = subprocess.run([sys.executable, "-c", RUN.format(root=str(ROOT), patch=patch, target=target)],
                          cwd=ROOT, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    if "MUTATION_STALE" in out:
        print(f"{name:34s} → строка не найдена — МУТАЦИЯ УСТАРЕЛА")
        return False
    if "no tests ran" in out or "not found" in out and "ERROR" in out:
        print(f"{name:34s} → тест не найден: {target}")
        return False
    red = proc.returncode != 0
    print(f"{name:34s} → {'КРАСНЫЙ — гард работает' if red else 'зелёный — ГАРД СЛЕПОЙ'}")
    return red


def main() -> int:
    ok = True
    for name, (mod, old, new, target) in SOURCE_MUTATIONS.items():
        ok &= _check(name, SRC_PATCH.format(mod=mod, old=old, new=new), target)
    for name, (patch, target) in ATTR_MUTATIONS.items():
        ok &= _check(name, patch, target)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
