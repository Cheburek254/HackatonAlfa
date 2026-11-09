"""Сравнение бейзлайна и recommend на одной разметке."""

from functools import partial

from app.recommender import recommend
from evaluation.runner import run_evaluation


def main() -> None:
    report = run_evaluation(partial(recommend, explain_with_llm=False))
    print(f"Сравнение на синтетическом каталоге. Сценариев: {report.scenario_count}.\n")
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
