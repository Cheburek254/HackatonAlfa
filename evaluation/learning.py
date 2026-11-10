"""Офлайн-проверки прогрессии, практической пользы, шума, дублей и свежести."""

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

from app.catalog import load_catalog
from app.catalog.normalizer import PgDumpReader
from app.contracts import Profile
from app.recommender.hybrid import configured_bm25_weight
from evaluation.catalog_quality import _semantic_vectors, catalog_fingerprint
from evaluation.learning_cases import (
    ADVANCED, BASIC, INTERMEDIATE, Scenario, bridge_cases, classify, freshness_cases, glossary_materials,
    history_mix, progression_cases, robustness_cases, theme_groups,
)
from evaluation.learning_briefs import BRIEFS
from evaluation.learning_report import REPORT_VERSION
from evaluation.quality_data import _topic, read_quality_data
from evaluation.stress import METHODS, make_ranker, relevance
from evaluation.stress_cases import SUITE_SIZE, sample_key

DUPLICATE_THRESHOLD = .85


def suite(title, description, cases, rank, measure, columns, *, titles):
    if len(cases) != SUITE_SIZE:
        raise ValueError(f"{title}: ожидалось {SUITE_SIZE} сценариев, получено {len(cases)}.")
    groups = defaultdict(list)
    for case in cases:
        groups[case.group].append((case, rank(case)))

    def row(label, data):
        return {"label": label, "count": len(data), "methods": [
            {"label": method, "values": measure([(case, orders[method]) for case, orders in data])} for method in METHODS
        ]}

    example = cases[0]
    return {
        "title": title, "description": description, "columns": columns,
        "overall": row("Все сценарии", [record for group in groups.values() for record in group]),
        "groups": [row(group, rows) for group, rows in groups.items()],
        "example": {"goal": example.profile.goal, "history": [titles[event.material_id] for event in example.history]},
    }


def progression_measure(levels):
    def measure(rows):
        novice = [(case, ids) for case, ids in rows if not case.avoid]
        graduated = [(case, ids) for case, ids in rows if case.avoid]
        slots = [levels[mid] for _, ids in novice for mid in ids]
        rated = [value for value in slots if value is not None]
        advanced = sum(value == ADVANCED for value in rated) / len(rated) if rated else None
        hits, mrr = relevance(rows)
        return [
            hits, mrr, advanced,
            mean(sum(levels[mid] in {BASIC, INTERMEDIATE} for mid in ids) >= 2 for _, ids in novice) if novice else None,
            mean(bool(case.avoid.intersection(ids)) for case, ids in graduated) if graduated else None,
            len(rated) / len(slots) if slots else None,
        ]
    return measure


def compare_variations(ranker, case):
    baseline = ranker(case.profile, case.history)
    noisy = ranker(case.profile, case.history + case.noise)
    shifted = ranker(case.profile, case.history + case.shift)
    return {method: (baseline[method][:3], noisy[method][:3], shifted[method][:3]) for method in METHODS}


def robustness_measure(rows):
    checks = []
    for case, (initial, noisy, shifted) in rows:
        stable = len(set(initial) & set(noisy) & case.targets) >= 2
        changed = len(set(shifted) & case.new_topic) >= 2 and len(set(initial) & case.new_topic) < 2
        checks.append((len(set(initial) & case.targets) >= 2, stable, changed, stable and changed))
    initially_relevant = [stable for initial, stable, _, _ in checks if initial]
    return [*(mean(values) for values in zip(*checks)),
            mean(initially_relevant) if initially_relevant else None]


def semantic_corpus(reader, data, materials):
    blocks = defaultdict(list)
    for row in reader.load_rows("GlossaryEmbedItems"):
        if row.get("HtmlContent"):
            blocks[row["GlossaryId"]].append(row["HtmlContent"])
    return _semantic_vectors(materials, data.text_blocks | {mid: " ".join(parts)[:8000] for mid, parts in blocks.items()})


def duplicate_cases(materials, vectors, groups):
    import numpy as np

    valid = [m for m in materials if m.id in vectors]
    matrix = np.stack([vectors[m.id] for m in valid])
    similarities = matrix @ matrix.T
    pool = defaultdict(list)
    left, right = np.where(np.triu(similarities > DUPLICATE_THRESHOLD, 1))
    for a, b in zip(left, right):
        first, second = valid[a], valid[b]
        different = first.entity_type != second.entity_type
        pool[different].append((first, second))
    styles = {
        "article": ("статья", "Дайте объяснение и пример, который смогу обсудить с коллегами."),
        "document": ("шаблон", "Хочу подготовить рабочий документ и потом передать его сотруднику."),
        "course": ("курс", "Задачу нужно освоить системно, чтобы позже обучить команду."),
        "webinar": ("вебинар", "Важно увидеть разбор и понять, какие решения мы можем применить."),
        "podcast": ("подкаст", "В дороге хочу послушать разбор и выписать идеи."),
        "glossary": (None, "Нужно коротко сверить значение термина перед встречей."),
    }
    used = set()
    seed_titles = Counter()
    for different in (True, False):
        count = 0
        for first, second in sorted(pool[different], key=lambda pair: sample_key(pair[0].id, pair[1].id)):
            if first.id in used or second.id in used:
                continue
            target = (first, second)[count % 2]
            title = re.split(r"[:?]", target.title, maxsplit=1)[0].strip()
            key = " ".join(re.sub(r"\d+", "", _topic(title)).split()[:2])
            if len(title) < 4 or seed_titles[key] >= 2:
                continue
            kind, action = styles.get(target.entity_type, (None, "Ищу практический разбор для следующего шага."))
            index = count % len(groups)
            prompt = f"На следующем этапе предстоит разобраться с темой «{title}». {action}"
            profile = Profile(goal=prompt, preferred_format=kind)
            history = history_mix(groups, index, count % 5, primary_count=3, noise=True)
            # Все близкие материалы того же формата подходят для предпочтительного слота.
            candidates = {target.id}
            for neighbor, similarity in zip(valid, matrix @ vectors[target.id]):
                if neighbor.entity_type == target.entity_type and similarity > DUPLICATE_THRESHOLD:
                    candidates.add(neighbor.id)
            yield Scenario("Разные форматы" if different else "Один формат", profile, history, frozenset(candidates))
            used.update((first.id, second.id))
            seed_titles[key] += 1
            count += 1
            if count == SUITE_SIZE // 2:
                break


def duplicate_measure(vectors):
    def measure(rows):
        comparisons = [[float(vectors[a] @ vectors[b]) for a, b in combinations(ids, 2) if a in vectors and b in vectors]
                       for _, ids in rows]
        complete = [values for values in comparisons if len(values) == 3]
        return [
            mean(max(values) > DUPLICATE_THRESHOLD for values in complete) if complete else None,
            mean(max(values) for values in complete) if complete else None,
            mean(bool(case.targets.intersection(ids)) for case, ids in rows),
            len(complete) / len(rows),
        ]
    return measure


def compare_freshness(original, balanced, case):
    normal = original(case.profile, case.history)
    neutral = balanced(case.profile, case.history)
    target = next(iter(case.targets))

    def wins(order):
        for mid in order:
            if mid == target:
                return True
            if mid == case.control:
                return False
        return None

    return {method: (normal[method][:3], wins(normal[method]), wins(neutral[method])) for method in METHODS}


def freshness_measure(rows):
    comparable = [(won, neutral) for _, (_, won, neutral) in rows if won is not None and neutral is not None]
    return [
        mean(bool(case.targets.intersection(ids)) for case, (ids, _, _) in rows),
        mean(case.control in ids for case, (ids, _, _) in rows),
        mean(won for won, _ in comparable) if comparable else None,
        mean(won for _, won in comparable) if comparable else None,
        len(comparable) / len(rows),
    ]


def build_report(backup, catalog_path):
    bm25_weight = configured_bm25_weight()
    catalog = load_catalog(catalog_path)
    reader = PgDumpReader(backup)
    quality = read_quality_data(backup, catalog, 0)
    glossary = glossary_materials(reader)
    all_materials = catalog + glossary
    titles = {m.id: m.title for m in all_materials}
    levels = {m.id: classify(m) for m in all_materials}
    groups = theme_groups(catalog)
    vectors = semantic_corpus(reader, quality, all_materials)
    current = make_ranker(catalog, catalog_path.parent, bm25_weight=bm25_weight)
    with_glossary = make_ranker(all_materials, catalog_path.parent, bm25_weight=bm25_weight)
    balanced = make_ranker(tuple(replace(m, popularity=None, rating=None) for m in sorted(catalog, key=lambda m: m.id)),
                           catalog_path.parent, bm25_weight=bm25_weight)

    def top(ranker, case):
        orders = ranker(case.profile, case.history)
        return {method: orders[method][:3] for method in METHODS}

    fresh = list(freshness_cases(reader, catalog, groups, quality.topics))
    by_id = {m.id: m for m in catalog}
    old_signals = sum(bool(by_id[case.control].rating is not None or by_id[case.control].popularity) for case in fresh)
    suites = [
        suite("Педагогическая прогрессия",
              "30 новичков с историей вводных статей и 20 сценариев после подтверждённого завершения всех уроков курса. "
              "История до завершения дополнена смоделированными интересами. У новичков проверяем большинство базовых или средних "
              "карточек и долю продвинутых среди размеченных слотов ниже 10%. У выпускников проверяем повтор базового глоссария по той же теме. "
              "Уровни получены по меткам, формату и ручным правилам; неизвестные уровни показаны через покрытие.",
              list(progression_cases(reader, catalog, glossary, groups, levels)), lambda case: top(with_glossary, case),
              progression_measure(levels), ["Hit@3", "MRR@3", "Продвинутые ↓", "2+ базовых/средних ↑", "Откат к глоссарию ↓", "Покрытие уровней"], titles=titles),
        suite("От теории к инструменту",
              "50 авторских запросов по 10 задачам. В истории два или три недавних прочтения по теме, старые интересы и случайный просмотр. "
              "Засчитываются размеченные калькуляторы и документы, пригодные для конкретного следующего шага.",
              list(bridge_cases(catalog, groups)), lambda case: top(current, case), relevance, ["Hit@3", "MRR@3"], titles=titles),
        suite("Устойчивость к случайным кликам",
              "25 задач с устойчивым интересом и слабой подсказкой в запросе, 25 задач с конкурирующими интересами. "
              "Для каждого профиля сравниваются исходная выдача, выдача после одного постороннего клика и после двух "
              "или трёх действий на новой теме. Стабильность требует минимум две прежние карточки по основной теме.",
              list(robustness_cases(catalog, groups)), lambda case: compare_variations(current, case), robustness_measure,
              ["Исходно 2+ по теме", "Ядро после мисклика ↑", "Переход после 2–3 ↑", "Оба условия ↑",
               "Ядро среди исходно релевантных"], titles=titles),
        suite("Семантические дубли",
              "25 плотных пар в разных форматах и 25 в одном формате. Глоссарий добавлен в офлайн-корпус. "
              "Для полных списков проверяется максимум косинусного сходства по TF-IDF и SVD из заголовков, описаний и текстовых блоков. "
              "Порог дублирования строго выше 0,85. Пары похожего содержания могут включать разные полезные документы.",
              list(duplicate_cases(all_materials, vectors, groups)), lambda case: top(with_glossary, case), duplicate_measure(vectors),
              ["Списки с дублем ↓", "Средний максимум косинуса", "Подходящий формат ↑", "Полный топ-3 с векторами"], titles=titles),
        suite("Свежесть и новые эксперты",
              "25 материалов из 12,5% последних публикаций и 25 материалов экспертов с первым поздним появлением в выгрузке и не более двумя "
              "оценками на связанных карточках. Контроль — более старая карточка того же формата с общей темой. "
              f"Из 50 контрольных карточек у {old_signals} есть рейтинг или ненулевая популярность. "
              "В контрфактуальном прогоне эти два сигнала отключены на всём каталоге. "
              "Порядок сравнивается на одних и тех же парах, где в обоих прогонах найден хотя бы один материал.",
              fresh, lambda case: compare_freshness(current, balanced, case), freshness_measure,
              ["Цель в топ-3", "Контроль в топ-3", "Цель выше контроля", "Цель выше без сигналов", "Сопоставимые пары"], titles=titles),
    ]
    return {
        "version": REPORT_VERSION, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "catalog_fingerprint": catalog_fingerprint(catalog), "suites": suites, "bm25_weight": bm25_weight,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("backup", "catalog", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.backup, args.catalog)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.output.parent) as temporary:
        staged = Path(temporary) / args.output.name
        staged.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.replace(args.output)
    print(f"Отчёт сохранён в {args.output}")


if __name__ == "__main__":
    main()
