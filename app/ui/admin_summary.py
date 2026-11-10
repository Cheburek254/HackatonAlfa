"""Рабочие критерии для краткой оценки сохранённых офлайн-прогонов."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    suite: str
    metric: str
    label: str
    threshold: float
    lower_is_better: bool = False
    group: str | None = None


STRESS_RULES = (
    Rule("Базовая точность · следующий курс", "Hit@3", "Предсказание следующего курса", .20),
    Rule("Учёт негатива", "Кейсы с нежелательным ↓", "Нежелательные рекомендации", .20, True),
    Rule("Смена контекста", "Hit@3", "Смена контекста", .50),
    Rule("Редкие форматы и отрасли", "Hit@3", "Редкие форматы и отрасли", .70),
    Rule("Холодный старт", "Критерий выполнен ↑", "Холодный старт", .70),
)
LEARNING_RULES = (
    Rule("Педагогическая прогрессия", "2+ базовых/средних ↑", "Уровень карточек для новичков", .90, group="Первые шаги"),
    Rule("От теории к инструменту", "Hit@3", "Переход к инструменту", .50),
    Rule("Устойчивость к случайным кликам", "Переход после 2–3 ↑", "Реакция на новый интерес", .50),
    Rule("Семантические дубли", "Списки с дублем ↓", "Выдачи с семантическим дублем", .10, True),
)


def _scores(rows, method):
    return next((row["values"] for row in rows if row["label"] == method), ())


def _percent(value):
    return f"{100 * value:.1f}%".replace(".", ",")


def build_assessments(quality, stress, learning):
    weak, strong, observations = [], [], []
    for report, rules, anchor in ((stress, STRESS_RULES, "admin-stress"), (learning, LEARNING_RULES, "admin-learning")):
        if not report:
            continue
        suites = {suite["title"]: suite for suite in report["suites"]}
        for rule in rules:
            suite = suites.get(rule.suite)
            if not suite:
                continue
            result = suite["overall"] if rule.group is None else next(
                (row for row in suite["groups"] if row["label"] == rule.group), None,
            )
            if result is None:
                continue
            scores = dict(zip(suite["columns"], _scores(result["methods"], "recommend")))
            value = scores.get(rule.metric)
            if value is None:
                continue
            passes = value <= rule.threshold if rule.lower_is_better else value >= rule.threshold
            target = "не выше" if rule.lower_is_better else "не ниже"
            count = result["count"]
            (strong if passes else weak).append({
                "label": rule.label, "value": _percent(value), "anchor": anchor,
                "explanation": f"Метрика {rule.metric.rstrip(' ↑↓')} для recommend. Ориентир {target} {_percent(rule.threshold)}. "
                               f"Проверено сценариев — {count}.",
            })

    if quality and (paywall := quality.get("synthetic_paywall")):
        rows = [row for row in paywall["rows"] if row["mode"] == "Рабочий фильтр выдачи"]
        if rows:
            blocked = sum(row["any_restricted"]["numerator"] for row in rows)
            count = sum(row["any_restricted"]["denominator"] for row in rows)
            (weak if blocked else strong).append({
                "label": "Соблюдение прав доступа", "value": f"{blocked} / {count} недоступных выдач",
                "anchor": "admin-paywall",
                "explanation": "Ориентир — ни одной выдачи с недоступной карточкой. Проверены синтетические профили с Курс+ и без него.",
            })

    if stress:
        graph = _scores(stress["graph"]["methods"], "recommend")
        if graph and graph[0] is not None:
            (weak if graph[0] < .20 else strong).append({
                "label": "Обобщение экспертных связей", "value": _percent(graph[0]),
                "anchor": "admin-stress",
                "explanation": f"Hit@3 по всему каталогу на скрытых связях. Ориентир не ниже 20%. "
                               f"Среди десяти отвлекающих статей целевой материал занял первое место в {_percent(graph[2])} кейсов.",
            })
        real = next((row for row in stress["previous"] if row["label"] == "Реальный каталог"), None)
        if real:
            current = _scores(real["methods"], "recommend")
            hybrid = _scores(real["methods"], "BM25 + recommend")
            if current and hybrid and current[0] is not None and hybrid[0] is not None:
                observations.append({
                    "label": "Точность на авторских запросах", "value": _percent(current[0]),
                    "anchor": "admin-stress",
                    "explanation": f"Hit@3 на {real['labeled']} запросах, MRR@3 — {current[1]:.3f}. "
                                   f"Офлайн-гибрид набрал {_percent(hybrid[0])} по Hit@3.",
                })

    if learning:
        fresh = next((row for row in learning["suites"] if row["title"] == "Свежесть и новые эксперты"), None)
        if fresh:
            scores = _scores(fresh["overall"]["methods"], "recommend")
            if scores and scores[0] is not None:
                observations.append({
                    "label": "Свежие материалы и новые эксперты", "value": _percent(scores[0]),
                    "anchor": "admin-learning",
                    "explanation": "Доля попаданий целевой карточки в топ-3. Для оценки влияния популярности пока мало контрольных карточек с этим сигналом.",
                })

    return [
        {"label": "Требует внимания", "tone": "needs-attention", "rows": weak},
        {"label": "Соответствует рабочему ориентиру", "tone": "meets-target", "rows": strong},
        {"label": "Сравнение и ограничения данных", "tone": "observations", "rows": observations},
    ]
