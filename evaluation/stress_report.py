"""Чтение агрегатов и проверка доступности выдачи для Курс+."""

import json

from app.contracts import Profile
from app.catalog.subscriptions import entitled_catalog
from app.recommender import recommend
from app.recommender.hybrid import B, FIELD_WEIGHTS, K1

REPORT_NAME = "recommendation-stress.json"
REPORT_VERSION = 2


def read_report(path, fingerprint, bm25_weight=None, *, adaptive=None):
    if path is None or fingerprint is None:
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(report, dict) and report.get("version") == REPORT_VERSION
            and report.get("catalog_fingerprint") == fingerprint
            and isinstance(report.get("suites"), list) and len(report["suites"]) == 5
            and (bm25_weight is None or report.get("bm25_weight") == bm25_weight)
            and (adaptive is None or bool(report.get("bm25_adaptive")) == adaptive)
            and isinstance(report.get("graph"), dict)):
        return report
    return None


def check_subscriptions(catalog, bm25_weight=0.0, *, bm25_field_weights=FIELD_WEIGHTS, bm25_length_penalty=B,
                        bm25_saturation=K1, bm25_adaptive=False):
    members = {m.id for m in catalog if "Курс+" in m.subscriptions}
    target = next((m for m in catalog if m.id in members and m.title == "Как управлять командой"), None)
    if target is None:
        return None

    rows = []
    profile = Profile(goal=target.title)
    for subscribed in (False, True):
        candidates = {m.id: m for m in entitled_catalog(catalog, subscribed)}
        result = recommend(tuple(candidates.values()), profile, (), 3, explain_with_llm=False,
                           use_expert_links=False, bm25_weight=bm25_weight, bm25_field_weights=bm25_field_weights,
                           bm25_length_penalty=bm25_length_penalty, bm25_saturation=bm25_saturation,
                           bm25_adaptive=bm25_adaptive)
        ids = tuple(item.material_id for item in result.items)
        rows.append({
            "label": "С подпиской Курс+" if subscribed else "Без подписки",
            "candidates": len(candidates),
            "passed": len(ids) == 3 and set(ids) <= candidates.keys() and (target.id in ids) == subscribed,
            "items": [{"title": candidates[mid].title, "member": mid in members} for mid in ids if mid in candidates],
        })
    return {"members": len(members), "goal": profile.goal, "rows": rows}
