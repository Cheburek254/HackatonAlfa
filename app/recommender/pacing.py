"""Мягкий баланс доступа в первых шести рекомендациях."""

from app.catalog import material_format
from app.catalog.subscriptions import access_category
from app.contracts import Material

RankedMaterial = tuple[float, Material, dict]
MONETIZED = {"paid", "subscription"}
FIRST_PAGE = 6
LOOKAHEAD = 18


def balance_first_page(
    ranked: list[RankedMaterial], *, paid_target: int, seen: set[str],
) -> list[RankedMaterial]:
    """Переставляет только близкие по релевантности варианты. Остальные сохраняет в выдаче."""
    if len(ranked) <= 3:
        return ranked
    order = ranked.copy()

    def comparable(candidate: RankedMaterial, incumbent: RankedMaterial) -> bool:
        score, _, signals = candidate
        current_score, _, current_signals = incumbent
        return (
            score >= current_score - max(3, abs(current_score) * .25)
            and signals["experience_score"] >= 0
            and (not current_signals["goal_direct"] or signals["goal_direct"])
        )

    paid_count = 0
    for position in range(min(FIRST_PAGE, len(order))):
        row = order[position]
        if access_category(row[1]) not in MONETIZED:
            continue
        if position >= 3 and paid_count >= paid_target:
            free_index = next((
                index for index in range(position + 1, min(len(order), LOOKAHEAD))
                if access_category(order[index][1]) == "free" and comparable(order[index], row)
            ), None)
            if free_index is not None:
                order[position], order[free_index] = order[free_index], order[position]
                continue
        paid_count += 1

    for position in range(3, FIRST_PAGE):
        if paid_count >= paid_target or position >= len(order):
            break
        if access_category(order[position][1]) != "free":
            continue
        paid_index = next((
            index for index in range(FIRST_PAGE, min(len(order), LOOKAHEAD))
            if access_category(order[index][1]) in MONETIZED
            and (order[index][1].entity_type == "course" or material_format(order[index][1]) == "курс")
            and order[index][1].id not in seen
            and comparable(order[index], order[position])
            and (order[index][2]["goal_direct"] or order[index][2]["matched_learning_topic"]
                 or order[index][2]["history_continuation_from"])
        ), None)
        if paid_index is not None:
            order[position], order[paid_index] = order[paid_index], order[position]
            paid_count += 1
    return order
