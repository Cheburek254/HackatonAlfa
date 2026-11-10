"""Сбор агрегированного отчёта качества из каталога и бэкапа организаторов."""

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from html import unescape
from itertools import combinations
from math import log
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

from app.catalog import load_catalog
from app.catalog.subscriptions import entitled_catalog
from app.contracts import InteractionEvent, Material, Profile
from app.recommender import recommend
from app.recommender.engine import _tokenize_stems
from app.recommender.onboarding import ONBOARDING_ITEMS
from evaluation.quality_data import read_quality_data
from evaluation.hybrid import constraints
from evaluation.runner import TOP_K, _load_cases, run_evaluation

REPORT_NAME = "catalog-quality.json"
REPORT_VERSION = 1
TYPE_LABELS = {
    "article": "Статьи", "course": "Курсы", "lesson": "Уроки", "webinar": "Вебинары",
    "document": "Документы", "podcast": "Подкасты", "calculator": "Калькуляторы",
}
FORMAT_TYPES = {"статья": "article", "курс": "course", "урок": "lesson", "вебинар": "webinar",
                "шаблон": "document", "подкаст": "podcast", "калькулятор": "calculator"}


def catalog_fingerprint(catalog: tuple[Material, ...]) -> str:
    payload = [(m.id, m.title, m.url, m.description, m.topics, m.format, m.access, m.entity_type,
                m.stages, m.industries, m.experience_levels, m.popularity, m.rating, m.cost, m.subscriptions)
               for m in catalog]
    return sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def read_report(path: Path | None, fingerprint: str | None) -> dict | None:
    if path is None or fingerprint is None:
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(report, dict) and report.get("version") == REPORT_VERSION
            and report.get("catalog_fingerprint") == fingerprint and isinstance(report.get("generated_at"), str)
            and all(isinstance(report.get(key), dict) for key in ("catalog", "history", "cold_start", "expert"))):
        return report
    return None


def _type(material: Material) -> str:
    return material.entity_type or FORMAT_TYPES.get(material.format, "other")


def _gini(frequencies: list[int]) -> float | None:
    total = sum(frequencies)
    if not total:
        return None
    n = len(frequencies)
    return sum((2 * i - n - 1) * count for i, count in enumerate(sorted(frequencies), 1)) / (n * total)


def _semantic_vectors(catalog: tuple[Material, ...], text_blocks: dict[str, str]):
    """Латентные векторы TF-IDF + усечённое SVD. NumPy нужен только при генерации отчёта."""
    import numpy as np

    def stems(html_text):
        return _tokenize_stems(unescape(re.sub(r"<[^>]*>", " ", html_text)))

    texts = []
    df = Counter()
    for item in catalog:
        terms = Counter({term: 1 for term in stems(item.description) | stems(text_blocks.get(item.id, ""))})
        terms.update({term: 2 for term in stems(item.title + " " + " ".join(item.topics))})
        texts.append(terms)
        df.update(terms.keys())
    vocabulary = sorted((term for term, count in df.items() if count > 1), key=lambda term: (-df[term], term))[:4000]
    if not vocabulary:
        return {}
    columns = {term: column for column, term in enumerate(vocabulary)}
    weights = {term: 1 + log((len(catalog) + 1) / (df[term] + 1)) for term in vocabulary}
    matrix = np.zeros((len(catalog), len(vocabulary)), dtype=np.float32)
    for row, terms in enumerate(texts):
        for term, count in terms.items():
            if term in columns:
                matrix[row, columns[term]] = count * weights[term]
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= np.maximum(norms, 1e-12)
    # Проекция с фиксированным seed ускоряет SVD для небольшого корпуса.
    projection = np.random.default_rng(0).standard_normal((len(vocabulary), 56), dtype=np.float32)
    basis, _ = np.linalg.qr(matrix @ projection)
    _, _, right = np.linalg.svd(basis.T @ matrix, full_matrices=False)
    vectors = matrix @ right[:48].T
    lengths = np.linalg.norm(vectors, axis=1)
    return {item.id: vector / length for item, vector, length in zip(catalog, vectors, lengths) if length > 1e-12}


def _ratio(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def paywall_cases(catalog):
    members = [m for m in catalog if "Курс+" in m.subscriptions]
    separate = next((m for m in catalog if m.access == "paid" and not m.subscriptions), None)
    probes = (members[:9] + [separate]) if separate else members[:10]
    authored = [Profile(**row["profile"]) for row in _load_cases("real") if row["profile"].get("goal")]
    ordinary = list(dict.fromkeys(p for p in authored if constraints(p.goal)[1:] == (None, None)))
    # Профили заданы вручную, события пользователей здесь не используются.
    ordinary.sort(key=lambda profile: sha256(profile.goal.encode()).digest())
    return {
        "Обычные задачи": [(p, None) for p in ordinary[:50 - 2 * len(probes)]],
        "Запрос конкретного платного материала": [(Profile(goal=m.title), m.id) for m in probes],
        "Та же тема с условием «только бесплатно»": [
            (Profile(goal=f"По теме «{m.title}» нужны только бесплатные материалы."), None) for m in probes
        ],
    }


def synthetic_paywall(catalog, rank):
    """Парные профили с одинаковым запросом и без покупок отдельных материалов."""
    members = [m for m in catalog if "Курс+" in m.subscriptions]
    groups = paywall_cases(catalog)
    def top(candidates, profile):
        return tuple(item.material_id for item in rank(candidates, profile, (), TOP_K).items)

    unfiltered = {profile: top(catalog, profile) for cases in groups.values() for profile, _ in cases}
    rows, breakdown = [], {label: [] for label in groups}
    for subscribed, plan in ((False, "Без подписки"), (True, "С подпиской Курс+")):
        candidates = entitled_catalog(catalog, subscribed)
        allowed = {material.id for material in candidates}
        filtered = {profile: top(candidates, profile) for profile in unfiltered}
        for mode, orders in (("Рабочий фильтр выдачи", filtered), ("Сравнение без фильтра", unfiltered)):
            def summarize(cases):
                nonempty = []
                target_checks = []
                for profile, target in cases:
                    mids = orders[profile]
                    if mids:
                        nonempty.append((len(mids), sum(mid not in allowed for mid in mids)))
                    if target in allowed:
                        target_checks.append(target in mids)
                return {
                    "plan": plan, "mode": mode,
                    "any_restricted": _ratio(sum(blocked > 0 for _, blocked in nonempty), len(cases)),
                    "all_restricted": _ratio(sum(total == blocked for total, blocked in nonempty), len(nonempty)),
                    "restricted_slots": _ratio(sum(blocked for _, blocked in nonempty), sum(total for total, _ in nonempty)),
                    "empty": _ratio(len(cases) - len(nonempty), len(cases)),
                    "requested_available": _ratio(sum(target_checks), len(target_checks)),
                }

            rows.append(summarize([case for cases in groups.values() for case in cases]))
            for label, cases in groups.items():
                breakdown[label].append(summarize(cases))
    return {"profiles": len(unfiltered), "course_plus": len(members), "rows": rows, "groups": breakdown}


def build_quality_report(backup: Path, catalog_path: Path, sample_size: int) -> dict:
    catalog = load_catalog(catalog_path)
    data = read_quality_data(backup, catalog, sample_size)
    current = partial(recommend, links_directory=catalog_path.parent, explain_with_llm=False)
    materials = {item.id: item for item in catalog}

    def top(profile, history=(), algorithm=current):
        return tuple(item.material_id for item in algorithm(catalog, profile, history, TOP_K).items)

    evaluation = run_evaluation(current, suite="real", real_catalog=catalog)
    authored = [row.material_ids for row in evaluation.rows if row.method == "recommend"]
    shown = Counter(mid for ids in authored for mid in ids)
    category_totals = Counter(map(_type, catalog))
    category_shown = Counter(_type(materials[mid]) for mid in shown)
    unseen = materials.keys() - data.observed_items
    vectors = _semantic_vectors(catalog, data.text_blocks)
    distances = [1 - max(-1., min(1., float(vectors[a] @ vectors[b]))) for ids in authored
                 for a, b in combinations(ids, 2) if a in vectors and b in vectors]
    nonempty = [ids for ids in authored if ids]

    replay_lists = [(case, top(Profile(), case.history)) for case in data.replay]
    replay_slots = [mid for _, ids in replay_lists for mid in ids]
    completed_lists = [(case, ids) for case, ids in replay_lists if case.completed_courses and ids]
    lesson_lists = [(case, ids) for case, ids in replay_lists if case.completed_lessons and ids]
    rated_lists = [(case, ids) for case, ids in replay_lists if case.liked_topics and ids]
    paid_only = sum(bool(ids) and all(materials[mid].access == "paid" for mid in ids) for _, ids in replay_lists)

    # Граф в этой проверке служит эталоном и исключён из входов ранжировщика.
    without_graph = partial(current, use_expert_links=False)
    expert_tops = [(targets, top(Profile(), (InteractionEvent(source, "click"),), without_graph))
                   for source, targets in sorted(data.expert.items())]

    article_popularity = sorted((item.popularity or 0) for item in catalog
                                if _type(item) == "article" and (item.popularity or 0) > 0)
    threshold = article_popularity[(3 * len(article_popularity)) // 4] if article_popularity else None

    def suitable_cold(item):
        introductory = ("начинающий" in item.experience_levels or "без опыта" in item.experience_levels
                        or bool({"идея", "запуск"}.intersection(item.stages)))
        popular_article = (_type(item) == "article" and threshold is not None
                           and (item.popularity or 0) >= threshold)
        return item.access == "free" and ((_type(item) == "course" and introductory) or popular_article)

    cold_profiles = [("Без анкеты", Profile())] + [
        (topic.capitalize(), Profile(stage="запуск", experience="начинающий", learning_topic=topic))
        for topic in ("запуск бизнеса", "маркетинг", "продажи", "финансы", "операционная работа")
    ]
    cold = []
    for name, profile in cold_profiles:
        items = [materials[mid] for mid in top(profile)]
        expected = tuple(item.material_id for item in ONBOARDING_ITEMS)
        package_matches = tuple(item.id for item in items) == expected
        cold.append({"profile": name, "items": [
            {"title": item.title, "suitable": package_matches if profile == Profile() else suitable_cold(item)}
            for item in items
        ]})

    return {
        "version": REPORT_VERSION, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "catalog_fingerprint": catalog_fingerprint(catalog),
        "catalog": {
            "size": len(catalog), "scenario_count": len(authored), "slots": shown.total(),
            "coverage": _ratio(len(shown), len(catalog)), "unseen": _ratio(len(shown.keys() & unseen), len(unseen)),
            "gini": _gini([shown[mid] for mid in materials]),
            "types": [{"label": TYPE_LABELS.get(key, "Другие"), **_ratio(category_shown[key], category_totals[key])}
                      for key in dict.fromkeys((*TYPE_LABELS, *category_totals))],
            "type_diversity": mean(len({_type(materials[mid]) for mid in ids}) for ids in nonempty) if nonempty else None,
            "semantic_diversity": mean(distances) if distances else None, "semantic_pairs": len(distances),
        },
        "history": {
            "sample": len(replay_lists), "eligible": data.eligible_users,
            "completed_lessons": _ratio(sum(bool(case.completed_lessons.intersection(ids)) for case, ids in lesson_lists),
                                         len(lesson_lists)),
            "completed_courses": _ratio(sum(mid in case.completed_courses for case, ids in completed_lists for mid in ids),
                                        sum(len(ids) for _, ids in completed_lists)),
            "completed_profiles": len(completed_lists),
            "subscription_records": data.course_bindings_with_subscription,
            "free_stress": _ratio(paid_only, sum(bool(ids) for _, ids in replay_lists)),
            "paid_slots": _ratio(sum(materials[mid].access == "paid" for mid in replay_slots), len(replay_slots)),
            "topic_relevance": _ratio(sum(bool(case.liked_topics & data.topics.get(mid, set()))
                                          for case, ids in rated_lists for mid in ids),
                                      sum(len(ids) for _, ids in rated_lists)),
            "rated_profiles": len(rated_lists),
        },
        "synthetic_paywall": synthetic_paywall(catalog, current),
        "cold_start": {"profiles": cold, "passed": _ratio(sum(len(row["items"]) == TOP_K and all(
            item["suitable"] for item in row["items"]) for row in cold), len(cold))},
        "expert": {
            "raw_edges": data.expert_raw_count, "edges": sum(map(len, data.expert.values())),
            "hit": _ratio(sum(bool(targets.intersection(ids)) for targets, ids in expert_tops), len(expert_tops)),
            "slots": _ratio(sum(len(targets.intersection(ids)) for targets, ids in expert_tops),
                            sum(len(ids) for _, ids in expert_tops)),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--history-sample", type=int, default=300)
    args = parser.parse_args()
    if args.history_sample < 1:
        parser.error("--history-sample должно быть положительным")
    report = build_quality_report(args.backup, args.catalog, args.history_sample)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.output.parent) as temporary:
        staged = Path(temporary) / args.output.name
        staged.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.replace(args.output)
    print(f"Отчёт сохранён в {args.output}")


if __name__ == "__main__":
    main()
