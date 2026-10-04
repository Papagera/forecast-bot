"""Фейки для офлайн-тестов: клиент Metaculus, транспорт LLM, вопросы всех типов."""
from __future__ import annotations

import ast
import re
from typing import Any

from forecasting_tools import (
    BinaryQuestion,
    DiscreteQuestion,
    MetaculusClient,
    MultipleChoiceQuestion,
    NumericQuestion,
)
from litellm import ModelResponse


def _post(pid: int, qid: int, qtype: str, title: str, extra: dict | None = None, forecasted: bool = False) -> dict:
    question = {
        "id": qid, "title": title, "status": "open", "type": qtype,
        "description": f"Фон вопроса {qid}", "resolution_criteria": "Критерий", "fine_print": "",
        "scheduled_close_time": "2026-10-09T00:00:00Z", "scheduled_resolve_time": "2026-10-10T00:00:00Z",
        "open_time": "2026-09-21T00:00:00Z",
        "my_forecasts": {"history": [{"start_time": 1, "end_time": None, "forecast_values": [0.5, 0.5]}] if forecasted else []},
    }
    question.update(extra or {})
    return {"id": pid, "question": question,
            "projects": {"tournament": [{"slug": "minibench"}], "default_project": {"id": 33125}}}


SCALING_NUM = {"range_min": 0.0, "range_max": 100.0, "zero_point": None, "inbound_outcome_count": None,
               "nominal_min": None, "nominal_max": None, "continuous_range": None}
SCALING_DISC = {"range_min": -0.5, "range_max": 10.5, "zero_point": None, "inbound_outcome_count": 11,
                "nominal_min": 0, "nominal_max": 10, "continuous_range": None}


def questions(forecasted_ids: tuple[int, ...] = ()) -> list:
    """`forecasted_ids` — вопросы, где у бот-аккаунта уже есть прогноз (флаг из my_forecasts.history)."""
    f = lambda q: q == 201 and q in forecasted_ids  # история с forecast_values разбирается только у binary  # noqa: E731
    qs = [
        BinaryQuestion.from_metaculus_api_json(_post(101, 201, "binary", "Будет ли X?", forecasted=f(201))),
        NumericQuestion.from_metaculus_api_json(_post(
            102, 202, "numeric", "Сколько будет Y?", forecasted=f(202),
            extra={"scaling": SCALING_NUM, "open_upper_bound": False, "open_lower_bound": False, "unit": "шт"})),
        DiscreteQuestion.from_metaculus_api_json(_post(
            103, 203, "discrete", "Сколько раз Z?", forecasted=f(203),
            extra={"scaling": SCALING_DISC, "open_upper_bound": False, "open_lower_bound": False, "unit": "раз"})),
        MultipleChoiceQuestion.from_metaculus_api_json(_post(
            104, 204, "multiple_choice", "Кто победит?", forecasted=f(204),
            extra={"options": ["Альфа", "Бета", "Гамма"], "group_variable": "Кандидат"})),
    ]
    for q in qs:
        if q.id_of_question in forecasted_ids:
            q.already_forecasted = True
    return qs


class FakeMetaculusClient(MetaculusClient):
    """Настоящая валидация payload из MetaculusClient, но вместо сети — запись в список."""

    def __init__(self, qs: list) -> None:
        super().__init__(token="fake-token")
        self._qs = qs
        self.predictions: list[tuple[int, dict]] = []
        self.comments: list[dict] = []

    def get_all_open_questions_from_tournament(self, tournament_id: Any) -> list:  # type: ignore[override]
        return list(self._qs)

    def _post_question_prediction(self, question_id: int, forecast_data: dict) -> None:  # type: ignore[override]
        self.predictions.append((question_id, forecast_data))

    def post_question_comment(self, post_id: int, comment_text: str, is_private: bool = True,
                              included_forecast: bool = True) -> None:  # type: ignore[override]
        self.comments.append({"post_id": post_id, "text": comment_text, "is_private": is_private})


class FakeLlm:
    """Транспорт вместо litellm.acompletion: отвечает по типу промпта шаблона."""

    def __init__(self, prompt_tokens: int = 1000, completion_tokens: int = 200,
                 billed_cost: float | None = None) -> None:
        self.calls: list[str] = []
        self.prompts: list[str] = []
        self.pt, self.ct = prompt_tokens, completion_tokens
        self.billed_cost = billed_cost  # как OpenRouter usage.cost (litellm кладёт в _hidden_params)
        self.kwargs: list[dict] = []
        self.tool_plan = ["search_news", "fred_series"]  # что «агент» вызовет по шагам
        self.hallucinate = False  # добавить в справку выдуманное число
        self.web_searches = 0

    async def __call__(self, *args: Any, messages: list | None = None, model: str = "", **kwargs: Any) -> ModelResponse:
        text = str(messages[-1]["content"]) if messages else ""
        self.calls.append(model)
        self.prompts.append(text)
        self.kwargs.append(kwargs)
        if kwargs.get("tools"):
            return self._agent_turn(model, messages, kwargs)
        if (kwargs.get("extra_body") or {}).get("plugins"):
            return self._web_search_turn(model)
        response = ModelResponse(
            model=model,
            choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": self._answer(text)}}],
            usage={"prompt_tokens": self.pt, "completion_tokens": self.ct, "total_tokens": self.pt + self.ct},
        )
        if self.billed_cost is not None:
            response._hidden_params = {"additional_headers": {"llm_provider-x-litellm-response-cost": self.billed_cost}}
        return response

    def _web_search_turn(self, model: str) -> ModelResponse:
        """Как живой ответ OpenRouter с плагином web (05.10.2026): аннотации url_citation, usage.cost = модель + Exa."""
        self.web_searches += 1
        anns = [{"type": "url_citation", "url_citation": {
            "url": f"https://example.org/news/{i}", "title": f"Заголовок {i}",
            "content": f"По данным на 4 октября, ставка составляет 3.75% (источник {i}).", "start_index": 0, "end_index": 0}}
            for i in (1, 2)]
        resp = ModelResponse(model=model, choices=[{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "ok", "annotations": anns}}],
            usage={"prompt_tokens": 4068, "completion_tokens": 40, "total_tokens": 4108, "cost": 0.010201,
                   "cost_details": {"upstream_inference_cost": 0.003201}})
        resp._hidden_params = {"additional_headers": {"llm_provider-x-litellm-response-cost": 0.010201}}
        return resp

    def _agent_turn(self, model: str, messages: list, kwargs: dict) -> ModelResponse:
        """Фейковый агент: по шагу на инструмент из tool_plan, затем итоговая справка."""
        done = sum(1 for m in messages if m.get("role") == "tool")
        usage = {"prompt_tokens": self.pt, "completion_tokens": self.ct, "total_tokens": self.pt + self.ct}
        if kwargs.get("tool_choice") == "auto" and done < len(self.tool_plan):
            name = self.tool_plan[done]
            arg = '{"query": "событие"}' if name == "search_news" else '{"series_id": "UNRATE"}'
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{done}", "type": "function", "function": {"name": name, "arguments": arg}}]}
            return ModelResponse(model=model, choices=[{"index": 0, "finish_reason": "tool_calls", "message": msg}],
                                 usage=usage)
        # Справка по правилу источников: строка-факт на каждый ответ инструмента с его [S#];
        # при hallucinate — ещё строка с числом, которого нет ни в одном источнике.
        lines = ["Справка агента: событие ещё не произошло."]
        for m in messages:
            if m.get("role") == "tool":
                head, _, body = str(m.get("content")).partition("\n")
                sid = head.split("]")[0].lstrip("[")
                lines.append(f"- {body[:60]} [{sid}]")
        if self.hallucinate:
            lines.append("- Рост составил 77.7% за квартал [S1]")
        brief = "\n".join(lines)
        return ModelResponse(model=model, usage=usage, choices=[
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": brief}}])

    @staticmethod
    def _answer(text: str) -> str:
        if "You are a data analyst helping to convert text" in text:
            if "BinaryPrediction" in text:
                return '{"prediction_in_decimal": 0.3}'
            if "PredictedOptionList" in text:
                m = re.search(r"option names are one of the following:\s*(\[.*?\])", text, re.S)
                opts = ast.literal_eval(m.group(1)) if m else []
                p = round(1 / len(opts), 6)
                items = ", ".join(f'{{"option_name": "{o}", "probability": {p}}}' for o in opts)
                return f'{{"predicted_options": [{items}]}}'
            if "Percentile" in text:
                m = re.search(r'between "?(-?[\d.]+)"?.*? and "?(-?[\d.]+)"?', text)
                lo, hi = (float(m.group(1)), float(m.group(2))) if m else (0.0, 100.0)
                pts = [(0.1, 0.2), (0.2, 0.3), (0.4, 0.45), (0.6, 0.55), (0.8, 0.7), (0.9, 0.8)]
                items = ", ".join(f'{{"percentile": {p}, "value": {round(lo + (hi - lo) * f, 4)}}}' for p, f in pts)
                return f"[{items}]"
            return "<<REQUESTED TYPE WAS NOT FOUND IN TEXT>>"
        if "You are an assistant to a superforecaster" in text:
            return "Свежие новости по вопросу: событие ещё не произошло."
        if "Please summarize the following research" in text:
            return "Сводка исследования."
        if '"Probability: ZZ%"' in text:
            return "Рассуждение: статус-кво держится.\nProbability: 30%"
        if "Option_A: Probability_A" in text:
            return "Рассуждение по вариантам.\nАльфа: 34%\nБета: 33%\nГамма: 33%"
        return ("Рассуждение по числу.\nPercentile 10: 20\nPercentile 20: 30\nPercentile 40: 45\n"
                "Percentile 60: 55\nPercentile 80: 70\nPercentile 90: 80")
