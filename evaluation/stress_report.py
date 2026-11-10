"""Чтение агрегатов и проверка доступности выдачи для Курс+."""

import json

from app.contracts import Profile
from app.catalog.subscriptions import entitled_catalog
from app.recommender import recommend

REPORT_NAME = "recommendation-stress.json"
REPORT_VERSION = 1


def read_report(path, fingerprint):
    if path is None or fingerprint is None:
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(report, dict) and report.get("version") == REPORT_VERSION
            and report.get("catalog_fingerprint") == fingerprint
            and isinstance(report.get("suites"), list) and len(report["suites"]) == 5
            and isinstance(report.get("previous"), list) and isinstance(report.get("graph"), dict)):
        return report
    return None


def check_subscriptions(catalog):
    members = {m.id for m in catalog if "Курс+" in m.subscriptions}
    target = next((m for m in catalog if m.id in members and m.title == "Как управлять командой"), None)
    if target is None:
        return None

    rows = []
    profile = Profile(goal=target.title)
    for subscribed in (False, True):
        candidates = {m.id: m for m in entitled_catalog(catalog, subscribed)}
        result = recommend(tuple(candidates.values()), profile, (), 3, explain_with_llm=False, use_expert_links=False)
        ids = tuple(item.material_id for item in result.items)
        rows.append({
            "label": "С подпиской Курс+" if subscribed else "Без подписки",
            "candidates": len(candidates),
            "passed": len(ids) == 3 and set(ids) <= candidates.keys() and (target.id in ids) == subscribed,
            "items": [{"title": candidates[mid].title, "member": mid in members} for mid in ids if mid in candidates],
        })
    return {"members": len(members), "goal": profile.goal, "rows": rows}
