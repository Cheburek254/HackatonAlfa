"""Офлайн-сравнение BM25, recommend и гибрида на пяти наборах и экспертном графе."""

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

from app.catalog import DEMO_CATALOG_PATH, load_catalog
from app.catalog.normalizer import PgDumpReader
from app.contracts import InteractionEvent, Profile
from app.recommender import recommend
from app.recommender.onboarding import ONBOARDING_ITEMS
from evaluation.catalog_quality import catalog_fingerprint
from evaluation.hybrid import acceptable, constraints, make_bm25, make_result, merge_orders
from evaluation.quality_data import read_quality_data
from evaluation.runner import EVALUATION_DIR, FIXTURES_DIR, TOP_K, _load_cases, run_evaluation
from evaluation.stress_cases import (
    SUITE_SIZE, behavioral_cases, cold_cases, context_cases, publication_dates, rare_cases, read_authors, sample_key,
)
from evaluation.stress_report import REPORT_VERSION

METHODS = ("recommend", "BM25", "BM25 + recommend")


def make_ranker(catalog, links_directory, *, contextual=True, graph=None, exclude_sources=False):
    text_order = make_bm25(catalog)
    materials = {m.id: m for m in catalog}

    @cache
    def ranks(profile, history, candidate_ids=None):
        seen = {event.material_id for event in history}
        query = profile.goal
        if contextual and not query:
            query = " ".join(
                part for part in (profile.industry, profile.learning_topic,
                                  *(materials[e.material_id].title for e in history[-2:] if e.material_id in materials))
                if part
            )
        candidates = catalog if candidate_ids is None else tuple(m for m in catalog if m.id in candidate_ids)
        result = recommend(candidates, profile, history, len(candidates), explain_with_llm=False,
                           links_directory=links_directory, expert_links=graph)
        current = tuple(r.material_id for r in result.items if not exclude_sources or r.material_id not in seen)
        bm25 = tuple(mid for mid in text_order(query) if (candidate_ids is None or mid in candidate_ids)
                     and (not contextual or mid not in seen))
        filters = constraints(profile.goal)
        inputs = (tuple(mid for mid in order if acceptable(materials[mid], filters)) for order in (bm25, current))
        hybrid = tuple(merge_orders(inputs))
        return dict(zip(METHODS, (current, bm25, hybrid)))

    return ranks


def rr(ids, targets):
    return next((1 / rank for rank, mid in enumerate(ids[:TOP_K], 1) if mid in targets), 0.0)


def metrics(scores):
    return {"hit": sum(score > 0 for score in scores) / len(scores), "mrr": mean(scores)}


def suite_result(title, description, cases, rank, measure, columns):
    if len(cases) != SUITE_SIZE:
        raise ValueError(f"{title}: ожидалось {SUITE_SIZE} сценариев, найдено {len(cases)}.")
    groups = defaultdict(list)
    for case in cases:
        groups[case.group].append((case, rank(case)))

    def report_group(name, rows):
        return {"label": name, "count": len(rows), "methods": [
            {"label": method, "values": measure([(case, orders[method][:TOP_K]) for case, orders in rows])}
            for method in METHODS
        ]}

    return {
        "title": title, "description": description, "columns": columns,
        "overall": report_group("Все сценарии", [row for rows in groups.values() for row in rows]),
        "groups": [report_group(name, rows) for name, rows in groups.items()],
    }


def relevance(rows):
    scores = metrics([rr(ids, case.targets) for case, ids in rows])
    return [scores["hit"], scores["mrr"]]


def negative_measure(topics, authors):
    def measure(rows):
        violations = exact = shared_tags = shared_authors = slots = 0
        for case, ids in rows:
            target = next(iter(case.targets))
            tags, names = topics.get(target, set()), authors.get(target, set())
            matches = [(mid == target, bool(tags & topics.get(mid, set())), bool(names & authors.get(mid, set())))
                       for mid in ids]
            violations += any(any(match) for match in matches)
            exact += sum(match[0] for match in matches)
            shared_tags += sum(match[1] for match in matches)
            shared_authors += sum(match[2] for match in matches)
            slots += len(ids)
        return [violations / len(rows), exact / slots if slots else None,
                shared_tags / slots if slots else None, shared_authors / slots if slots else None,
                sum(bool(ids) for _, ids in rows) / len(rows)]
    return measure


def cold_measure(catalog, topics):
    expected = tuple(item.material_id for item in ONBOARDING_ITEMS)
    materials = {m.id: m for m in catalog}

    def passed(case, ids):
        if case.targets:
            return bool(case.targets.intersection(ids))
        if not case.history:
            return ids == expected
        clicked = case.history[0].material_id
        clicked_topics = topics.get(clicked, set())
        return (len({materials[mid].entity_type for mid in ids}) >= 2 and clicked not in ids
                and any(topics.get(mid, set()) - clicked_topics for mid in ids))

    return lambda rows: [sum(passed(case, ids) for case, ids in rows) / len(rows)]


def compare_previous(catalog, directory):
    demo = load_catalog(DEMO_CATALOG_PATH) + load_catalog(EVALUATION_DIR / "catalog.extra.json")
    rows = []
    for suite, name, materials, links in (
        ("demo", "Синтетический каталог", demo, FIXTURES_DIR), ("real", "Реальный каталог", catalog, directory),
    ):
        ranks = make_ranker(materials, links, contextual=False)
        row = {"label": name, "methods": []}
        for method in METHODS:
            def evaluate(_, profile, history, limit):
                return make_result(ranks(profile, tuple(history))[method], limit, method)

            report = run_evaluation(evaluate, suite=suite, real_catalog=catalog)
            result = report.summaries[1]
            row["count"] = report.scenario_count
            row["labeled"] = result.eligible
            row["methods"].append({"label": method, "values": [result.hit_rate, result.mrr]})
        rows.append(row)
    return rows


def split_graph(expert):
    pairs = sorted({tuple(sorted((src, dst))) for src, targets in expert.items() for dst in targets},
                   key=lambda pair: sample_key(*pair))
    cutoff = round(.8 * len(pairs))
    retained = set(pairs[:cutoff])
    train, hidden = defaultdict(set), defaultdict(set)
    for src, targets in expert.items():
        for dst in targets:
            if tuple(sorted((src, dst))) in retained:
                train[src].add(dst)
                train[dst].add(src)
            else:
                hidden[src].add(dst)
    return {src: sorted(targets) for src, targets in train.items()}, hidden, cutoff, len(pairs) - cutoff


def compare_graph(catalog, data, directory):
    train, hidden, train_count, test_count = split_graph(data.expert)
    incoming = Counter(target for src, targets in data.expert.items() for target in targets if target in train.get(src, ()))
    articles = sorted((m for m in catalog if m.entity_type == "article"),
                      key=lambda m: (-(m.popularity or 0), -incoming[m.id], m.id))
    materials = {m.id: m for m in catalog}
    rank = make_ranker(catalog, directory, graph=train, exclude_sources=True)
    probes = []
    for src in sorted(hidden, key=sample_key):
        if not train.get(src):
            continue
        related = set(data.expert[src]) | set(train[src]) | {k for k, targets in hidden.items() if src in targets}
        for target in sorted(hidden[src], key=sample_key):
            tags = data.topics.get(src, set()) | data.topics.get(target, set())
            industries = set(materials[src].industries) | set(materials[target].industries)
            negatives = [m.id for m in articles if m.id not in related and m.id not in {src, target}
                         and not tags.intersection(data.topics.get(m.id, set()))
                         and not industries.intersection(m.industries)][:10]
            if len(negatives) == 10:
                probes.append((src, target, frozenset(negatives + [src, target])))
                break
        if len(probes) == SUITE_SIZE:
            break
    if len(probes) != SUITE_SIZE:
        raise ValueError(f"Экспертный граф: найдено только {len(probes)} проверок с 10 ложными кандидатами.")
    outcomes = [(target, rank(Profile(), (InteractionEvent(src, "click"),)),
                 rank(Profile(), (InteractionEvent(src, "click"),), candidates))
                for src, target, candidates in probes]
    rows = []
    for method in METHODS:
        scores = metrics([rr(full[method], {target}) for target, full, _ in outcomes])
        rows.append({"label": method, "values": [scores["hit"], scores["mrr"],
                     mean(orders[method][:1] == (target,) for target, _, orders in outcomes)]})
    return {
        "count": len(probes), "train_pairs": train_count, "held_out_pairs": test_count, "methods": rows,
        "measured_article_popularity": any((m.popularity or 0) > 0 for m in articles),
    }


def build_report(backup, catalog_path):
    catalog = load_catalog(catalog_path)
    data = read_quality_data(backup, catalog, 0)
    directory = catalog_path.parent
    reader = PgDumpReader(backup)
    published = publication_dates(reader)
    temporal, negative, eligible = behavioral_cases(reader, catalog, published)
    glossary, rare = rare_cases(reader, catalog, _load_cases("real"))
    drift = context_cases(catalog, data.topics)
    cold = cold_cases(catalog, data.topics)
    # Наблюдения после границы могут влиять на агрегаты популярности и связи. Убираем их и порядок экспорта.
    historical_catalog = tuple(replace(m, popularity=None, rating=None) for m in sorted(catalog, key=lambda m: m.id))
    current_rank = make_ranker(catalog, directory)
    rare_rank = make_ranker(catalog + glossary, directory)

    @cache
    def historical_ranker(available):
        return make_ranker(tuple(m for m in historical_catalog if m.id in available), directory, graph={})

    def temporal_orders(case):
        available = frozenset(m.id for m in historical_catalog if m.id in published and published[m.id] < case.cutoff)
        return historical_ranker(available)(case.profile, case.history)

    def current_orders(case):
        return current_rank(case.profile, case.history)

    precision = ["Hit@3", "MRR@3"]
    suites = [
        suite_result("Базовая точность · следующий курс",
                     f"50 из {eligible} подходящих историй. Скрыто последнее или предпоследнее действие "
                     "с ранее не встречавшимся курсом. Кандидаты опубликованы до скрытого действия. "
                     "На входе 4–9 более ранних действий. Уроки учтены как активность внутри курса. "
                     "Одновременные события, граф, популярность и рейтинги исключены из входа.",
                     temporal, temporal_orders, relevance, precision),
        suite_result("Учёт негатива",
                     "35 низких оценок и 15 случаев с прогрессом до 5% и паузой от 30 дней. "
                     "Сравниваются точное совпадение, теги и обезличенные ID авторов. "
                     f"История до негативного сигнала доступна в {sum(bool(case.history) for case in negative)} кейсах. "
                     "Пустая выдача учитывается отдельно. Малый прогресс служит косвенным сигналом.",
                     negative, current_orders, negative_measure(data.topics, read_authors(reader)),
                     ["Кейсы с нежелательным ↓", "Тот же материал ↓", "Общие теги ↓", "Общий автор ↓", "Выдача есть ↑"]),
        suite_result("Смена контекста",
                     "25 переходов от вводных к продвинутым материалам и 25 смен отрасли. "
                     "На входе 4 клика двухмесячной давности и 2 недавних. "
                     "Эталон размечен по меткам опыта и отрасли, ранее открытые материалы исключены из эталона.",
                     drift, current_orders, relevance, precision),
        suite_result("Редкие форматы и отрасли",
                     "30 кейсов редких форматов и 20 отраслевых. Часть кейсов взята из прежней разметки. "
                     "Термины глоссария добавлены к кандидатам только в этом офлайн-наборе.",
                     rare, lambda case: rare_rank(case.profile, case.history), relevance, precision),
        suite_result("Холодный старт",
                     "1 полностью пустой профиль проверяет точный онбординг-пакет. Для 24 профилей с отраслью "
                     "проверяется хотя бы одна бесплатная карточка отрасли. Для 25 профилей с одним кликом "
                     "проверяются минимум два формата, новый тематический тег и отсутствие повтора открытой карточки.",
                     cold, current_orders, cold_measure(catalog, data.topics), ["Критерий выполнен ↑"]),
    ]
    return {
        "version": REPORT_VERSION, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "catalog_fingerprint": catalog_fingerprint(catalog), "suites": suites,
        "previous": compare_previous(catalog, directory), "graph": compare_graph(catalog, data, directory),
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
