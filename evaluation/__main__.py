"""Сравнение бейзлайна и recommend на одной разметке."""

import json
from argparse import ArgumentParser
from functools import partial
from pathlib import Path

from app.catalog import catalog_source
from app.recommender import recommend
from app.recommender.hybrid import configured_bm25_weight
from evaluation.catalog_quality import catalog_fingerprint
from evaluation.runner import FIXTURES_DIR, SUITE_LABELS, load_real_catalog, run_evaluation, summary_payload


def main() -> None:
    parser = ArgumentParser(description="Сравнение популярных курсов, recommend и BM25 + recommend.")
    parser.add_argument("--suite", choices=SUITE_LABELS, default="demo", help="Набор сценариев")
    parser.add_argument("--catalog", type=Path, help="Путь к экспорту SQLite/JSON для реального набора")
    parser.add_argument("--bm25-weight", type=float, help="Вес BM25 в смешанном режиме от 0 до 1")
    parser.add_argument("--adaptive-bm25", action="store_true", help="Адаптивный вес как в каталоге")
    parser.add_argument("--summary-json", type=Path, help="Сохранить итоги трёх режимов в JSON")
    parser.add_argument("--include-cases", action="store_true", help="Добавить в JSON результаты размеченных сценариев")
    args = parser.parse_args()
    if args.catalog and args.suite != "real":
        parser.error("--catalog используется только вместе с --suite real")
    try:
        catalog = None
        links_directory = FIXTURES_DIR
        if args.suite == "real":
            source = args.catalog or catalog_source()
            catalog = load_real_catalog(source)
            links_directory = source.parent
        weight = configured_bm25_weight() if args.bm25_weight is None else args.bm25_weight
        if not 0 <= weight <= 1:
            parser.error("--bm25-weight должен быть в диапазоне от 0 до 1")
        offline = partial(recommend, explain_with_llm=False, links_directory=links_directory, balance_access=True)
        report = run_evaluation(offline, partial(offline, bm25_weight=weight, bm25_adaptive=args.adaptive_bm25),
                                suite=args.suite, real_catalog=catalog)
        if args.summary_json:
            summary = summary_payload(report, args.suite, weight, catalog_fingerprint(catalog) if catalog else None,
                                      include_cases=args.include_cases, adaptive=args.adaptive_bm25)
            args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, FileNotFoundError) as error:
        parser.exit(1, f"{error}\n")
    print(f"{SUITE_LABELS[args.suite]}. Сценариев: {report.scenario_count}.\n")
    print(f"Базовый вес BM25: {weight:.1%}. Вес recommend: {1 - weight:.1%}. "
          f"Режим: {'адаптивный' if args.adaptive_bm25 else 'фиксированный'}.\n")
    print("| Метод | Hit@3 | MRR@3 | Precision@3 | nDCG@3 | MAP@3 |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in report.summaries:
        hit = f"{row.hit_rate:.1%} ({row.hits}/{row.eligible})" if row.hit_rate is not None else "—"
        scores = [f"{value:.3f}" if value is not None else "—"
                  for value in (row.mrr, row.precision, row.ndcg, row.map_at_k)]
        print(f"| {row.method} | {hit} | {' | '.join(scores)} |")

    print("\n| Профиль | Метод | Топ-3 в порядке выдачи | Hit@3 | RR@3 |")
    print("| --- | --- | --- | ---: | ---: |")
    for row in report.rows:
        rank = row.reciprocal_rank
        hit = str(int(rank > 0)) if rank is not None else "—"
        rr = f"{rank:.3f}" if rank is not None else "—"
        print(f"| {row.case_id} | {row.method} | {', '.join(row.material_ids) or 'пусто'} | {hit} | {rr} |")
    print("\nСценарии без релевантных материалов исключены из метрик. Если для курсов нет значений популярности, бейзлайн помечен «—».")


if __name__ == "__main__":
    main()
