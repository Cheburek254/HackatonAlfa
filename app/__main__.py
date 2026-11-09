from argparse import ArgumentParser
from pathlib import Path

from app.catalog import DEMO_CATALOG_PATH, catalog_source, load_catalog, load_history
from app.contracts import Profile, Recommender
from app.recommender import recommend


def main(recommender: Recommender) -> None:
    parser = ArgumentParser(description="Подобрать материалы из каталога Альфа-Курса.")
    parser.add_argument("--goal", help="Текущая задача пользователя")
    parser.add_argument("--history", type=Path, help="Путь к JSON-журналу одного пользователя")
    args = parser.parse_args()

    catalog_path = catalog_source()
    catalog = load_catalog(catalog_path)
    materials_by_id = {material.id: material for material in catalog}
    history = load_history(args.history) if args.history else ()
    result = recommender(catalog, Profile(goal=args.goal), history, 3)

    print("Подборка на тестовом каталоге" if catalog_path == DEMO_CATALOG_PATH else "Подборка из каталога Альфа-Курса")
    if result.ordering_reason:
        print(result.ordering_reason)
    for item in result.items:
        material = materials_by_id[item.material_id]
        print(f"\n{material.title}\n{item.reason}\n{item.next_step}\n{material.url}")
    if result.follow_up_question:
        print(f"\n{result.follow_up_question}")


if __name__ == "__main__":
    main(recommend)
