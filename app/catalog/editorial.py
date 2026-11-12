"""Направленные редакторские рекомендации между карточками каталога."""

from collections import defaultdict

ENTITY_TYPES = {
    "8ede4179-0faf-407d-a4fc-1d94f9a63089": "article",
    "da1ad865-8605-4ab1-9f6c-4489a26fbc35": "document",
    "8b440b1a-00c3-423a-9bae-3376475eb203": "course",
    "ece4acf3-5dd3-47b9-9f08-99c0d6203cc7": "webinar",
    "7ac1c8c3-18b8-42ea-92ea-fcc10a6d496f": "calculator",
    "142c23e2-bf71-4a73-b12e-6b1b5e4d47cc": "podcast",
}


def editorial_links(rows, material_types):
    ordered = defaultdict(dict)
    for row in rows:
        source, target = row.get("Id"), row.get("ItemId")
        target_type = ENTITY_TYPES.get(row.get("ItemTypeId"))
        if source not in material_types or source == target or not target_type or material_types.get(target) != target_type:
            continue
        raw_position = row.get("Order")
        position = int(raw_position) if raw_position is not None and str(raw_position).lstrip("-").isdigit() else float("inf")
        ordered[source][target] = min(position, ordered[source].get(target, position))
    return {
        source: sorted(targets, key=lambda target: (targets[target], target))
        for source, targets in sorted(ordered.items())
    }
