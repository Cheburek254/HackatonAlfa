"""Сравнение бейзлайна и recommend на одной разметке."""

from argparse import ArgumentParser
from functools import partial
from pathlib import Path

from app.catalog import catalog_source
from app.recommender import recommend
from evaluation.runner import EvaluationDataError, FIXTURES_DIR, SUITE_LABELS, load_real_catalog, run_evaluation


def main() -> None:
    parser = ArgumentParser(description="Оценка тематического бейзлайна и recommend.")
    parser.add_argument("--suite", choices=SUITE_LABELS, default="demo", help="Набор сценариев")
    parser.add_argument("--catalog", type=Path, help="Путь к экспорту SQLite/JSON для реального набора")
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
        offline = partial(recommend, explain_with_llm=False, links_directory=links_directory)
        report = run_evaluation(offline, suite=args.suite, real_catalog=catalog)
    except (EvaluationDataError, FileNotFoundError) as error:
        parser.exit(1, f"{error}\n")
    print(f"{SUITE_LABELS[args.suite]}. Сценариев: {report.scenario_count}.\n")
    print("| Метод | Hit@3 | MRR@3 |")
    print("| --- | ---: | ---: |")
    for row in report.summaries:
        hit = f"{row.hit_rate:.1%} ({row.hits}/{row.eligible})" if row.hit_rate is not None else "—"
        mrr = f"{row.mrr:.3f}" if row.mrr is not None else "—"
        print(f"| {row.method} | {hit} | {mrr} |")

    print("\n| Профиль | Метод | Топ-3 в порядке выдачи | Hit@3 | RR@3 |")
    print("| --- | --- | --- | ---: | ---: |")
    for row in report.rows:
        rank = row.reciprocal_rank
        hit = str(int(rank > 0)) if rank is not None else "—"
        rr = f"{rank:.3f}" if rank is not None else "—"
        print(f"| {row.case_id} | {row.method} | {', '.join(row.material_ids) or 'пусто'} | {hit} | {rr} |")
    print("\nСценарии без релевантных материалов помечены «—» и исключены из обеих метрик.")


if __name__ == "__main__":
    main()
